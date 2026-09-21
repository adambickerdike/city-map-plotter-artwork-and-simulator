#!/usr/bin/env python3
"""Compile, inspect, export, and safely stream physical pen-plot jobs.

The plot job is the boundary between artwork and hardware.  It contains the
exact flattened millimetre geometry, pen order, motion profile, timing model,
source hash, and safety findings used by both the animated simulator and the
controller.  Device execution remains fail-closed until a real controller
profile and the repository's physical calibration gates are satisfied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plotsim import (  # noqa: E402
    Machine,
    NON_SIMULATABLE_SVG_CODES,
    Pen,
    Stroke,
    _length,
    _offset_geometry_warnings,
    load_plate,
    order_strokes,
    preflight_svg,
    simulate,
)

JOB_SCHEMA = "city-map-plotter/plot-job-v1"
PROFILE_SCHEMA = "city-map-plotter/device-profile-v1"
_SAFE_ID = re.compile(r"[^a-z0-9._-]+")
_GCODE_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_REVIEW_ONLY_BYPASS_CODES = {
    "unmeasured-pens",
    "uncalibrated-machine-timing",
}


class PlotJobError(ValueError):
    """An artwork, profile, job, or controller safety invariant failed."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not accepted")


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError) as exc:
        raise PlotJobError(f"could not read {label} {path}: {exc}") from exc


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _round(value: float, places: int = 6) -> float:
    rounded = round(float(value), places)
    return 0.0 if rounded == 0 else rounded


def _finite_point(point: Iterable[Any]) -> tuple[float, float]:
    values = list(point)
    if len(values) != 2:
        raise PlotJobError("plot point must contain exactly x and y")
    x, y = float(values[0]), float(values[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise PlotJobError("plot point coordinates must be finite")
    return x, y


def _pen_record(pen: Pen) -> dict[str, Any]:
    return {
        "id": pen.id,
        "label": pen.label,
        "ink": pen.ink,
        "colour": pen.colour,
        "nib_mm": pen.nib_mm,
        "nominal_nib_mm": pen.nominal_mm,
        "calibration_state": pen.calibration,
    }


def _geometry_bounds(strokes: Iterable[Stroke]) -> dict[str, float]:
    records = list(strokes)
    if not records:
        return {"min_x_mm": 0.0, "min_y_mm": 0.0, "max_x_mm": 0.0, "max_y_mm": 0.0}
    min_x = min(
        point[0] - stroke.nib_mm / 2 for stroke in records for point in stroke.points
    )
    min_y = min(
        point[1] - stroke.nib_mm / 2 for stroke in records for point in stroke.points
    )
    max_x = max(
        point[0] + stroke.nib_mm / 2 for stroke in records for point in stroke.points
    )
    max_y = max(
        point[1] + stroke.nib_mm / 2 for stroke in records for point in stroke.points
    )
    return {
        "min_x_mm": _round(min_x),
        "min_y_mm": _round(min_y),
        "max_x_mm": _round(max_x),
        "max_y_mm": _round(max_y),
    }


def _job_digest_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job)
    payload.pop("job_sha256", None)
    return payload


def _serialized_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            {inner: _round(value) for inner, value in item.items()}
            if isinstance(item, dict)
            else _round(item)
            if isinstance(item, float)
            else item
        )
        for key, item in stats.items()
    }


def compile_plot_job(
    svg: Path,
    machine: Machine,
    *,
    order: str = "optimised",
    strict_svg: bool = True,
    profile_binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compile one SVG to the deterministic program shared by UI and hardware."""

    if order not in {"document", "merged", "optimised"}:
        raise PlotJobError(f"unsupported stroke order {order!r}")
    if profile_binding is not None:
        if (
            not isinstance(profile_binding.get("id"), str)
            or not profile_binding["id"]
            or not re.fullmatch(r"[0-9a-f]{64}", profile_binding.get("sha256", ""))
        ):
            raise PlotJobError("device profile binding must contain an id and SHA-256")
    if not svg.is_file():
        raise PlotJobError(f"no such SVG: {svg}")
    preflight = preflight_svg(svg, machine)
    if strict_svg and preflight.errors:
        findings = "; ".join(
            f"{issue.code}: {issue.message}" for issue in preflight.errors[:8]
        )
        raise PlotJobError(f"strict SVG preflight failed: {findings}")
    non_simulatable = [
        issue for issue in preflight.errors if issue.code in NON_SIMULATABLE_SVG_CODES
    ]
    if non_simulatable:
        findings = "; ".join(
            f"{issue.code}: {issue.message}" for issue in non_simulatable
        )
        raise PlotJobError(f"SVG has no safe millimetre simulation: {findings}")

    try:
        strokes, page = load_plate(svg, machine)
    except ValueError as exc:
        raise PlotJobError(f"SVG path geometry could not be compiled: {exc}") from exc
    if not strokes:
        raise PlotJobError("SVG contains no plottable path strokes")
    groups = order_strokes(strokes, order)

    # Quantise once at the job boundary, then use those exact points for every
    # derived value.  The viewer, verifier, and hardware exporter consequently
    # reason about precisely the coordinates that reach the controller.
    serialized_groups: list[dict[str, Any]] = []
    canonical_groups: list[tuple[Pen, list[Stroke]]] = []
    stroke_count = 0
    for group_index, (pen, items) in enumerate(groups):
        if not items:
            continue
        serialized: list[dict[str, Any]] = []
        canonical_strokes: list[Stroke] = []
        for stroke in items:
            encoded_points: list[list[float]] = []
            for point in stroke.points:
                encoded = [_round(point[0]), _round(point[1])]
                if not encoded_points or encoded != encoded_points[-1]:
                    encoded_points.append(encoded)
            if len(encoded_points) < 2:
                continue
            canonical_points = [
                (float(point[0]), float(point[1])) for point in encoded_points
            ]
            canonical_strokes.append(
                Stroke(
                    stroke.layer,
                    pen,
                    canonical_points,
                    stroke.sid,
                    stroke.rev,
                )
            )
            serialized.append(
                {
                    "source_stroke_id": stroke.sid,
                    "layer": stroke.layer,
                    "reversed": stroke.rev,
                    "points_mm": encoded_points,
                    "length_mm": _round(_length(canonical_points), 4),
                }
            )
            stroke_count += 1
        if serialized:
            serialized_groups.append(
                {
                    "index": group_index,
                    "pen": _pen_record(pen),
                    "strokes": serialized,
                }
            )
            canonical_groups.append((pen, canonical_strokes))

    if not canonical_groups:
        raise PlotJobError("SVG paths collapse below plot-job coordinate precision")
    moves, stats = simulate(canonical_groups, machine)
    canonical_strokes = [stroke for _pen, items in canonical_groups for stroke in items]
    bounds = _geometry_bounds(canonical_strokes)

    safety: list[dict[str, str]] = []
    for issue in preflight.issues:
        if issue.severity in {"error", "warning"}:
            safety.append(issue.as_dict())
    for problem in _offset_geometry_warnings(page):
        safety.append(
            {
                "severity": "error",
                "code": "declared-geometry-mismatch",
                "message": problem,
            }
        )
    tolerance = 1e-6
    if (
        bounds["min_x_mm"] < -tolerance
        or bounds["min_y_mm"] < -tolerance
        or bounds["max_x_mm"] > page["width"] + tolerance
        or bounds["max_y_mm"] > page["height"] + tolerance
    ):
        safety.append(
            {
                "severity": "error",
                "code": "geometry-outside-page",
                "message": "Stroke ink envelope extends beyond the declared SVG page.",
            }
        )

    all_pens = [pen for pen, items in canonical_groups if items]
    unmeasured = [pen.id for pen in all_pens if not pen.measured]
    if unmeasured:
        safety.append(
            {
                "severity": "blocker",
                "code": "unmeasured-pens",
                "message": "Physical execution remains review-only; unmeasured pens: "
                + ", ".join(unmeasured),
            }
        )
    if machine.calibration_state not in {
        "measured",
        "timing-measured",
        "hardware-verified",
    }:
        safety.append(
            {
                "severity": "blocker",
                "code": "uncalibrated-machine-timing",
                "message": "Machine timing is nominal; record timed calibration plots first.",
            }
        )

    source_bytes = svg.read_bytes()
    job: dict[str, Any] = {
        "schema": JOB_SCHEMA,
        "source": {
            "name": svg.name,
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "bytes": len(source_bytes),
        },
        "page_mm": {"width": page["width"], "height": page["height"]},
        "title": page["title"],
        "order": order,
        "device_profile": dict(profile_binding) if profile_binding else None,
        "machine": machine.as_dict(),
        "preflight": preflight.as_dict(),
        "geometry": {
            "curve_flatness_mm": machine.curve_flatness_mm,
            "stroke_count": stroke_count,
            "vertex_count": sum(
                len(stroke["points_mm"])
                for group in serialized_groups
                for stroke in group["strokes"]
            ),
            "ink_bounds_mm": bounds,
        },
        "stats": _serialized_stats(stats),
        "safety": {
            "execution_allowed": not any(
                issue["severity"] in {"error", "blocker"} for issue in safety
            ),
            "findings": safety,
        },
        "pen_groups": serialized_groups,
        "move_count": len(moves),
    }
    job["job_sha256"] = _digest(_job_digest_payload(job))
    return job


def _job_number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise PlotJobError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlotJobError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        condition = "positive and finite" if positive else "finite"
        raise PlotJobError(f"{label} must be {condition}")
    return number


def _groups_from_job(job: dict[str, Any]) -> list[tuple[Pen, list[Stroke]]]:
    raw_groups = job.get("pen_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise PlotJobError("plot job has no pen groups")
    groups: list[tuple[Pen, list[Stroke]]] = []
    for group_index, group in enumerate(raw_groups):
        if not isinstance(group, dict):
            raise PlotJobError("plot job contains an invalid pen group")
        raw_pen = group.get("pen")
        raw_strokes = group.get("strokes")
        if not isinstance(raw_pen, dict) or not isinstance(raw_strokes, list):
            raise PlotJobError("plot job contains an invalid pen group")
        if not raw_strokes:
            raise PlotJobError("plot job contains an empty pen group")
        text_fields: dict[str, str] = {}
        for field in ("id", "label", "ink", "colour", "calibration_state"):
            value = raw_pen.get(field)
            if not isinstance(value, str) or not value:
                raise PlotJobError(
                    f"plot job pen group {group_index} has invalid {field}"
                )
            text_fields[field] = value
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text_fields["id"]) is None:
            raise PlotJobError(f"plot job pen group {group_index} has an unsafe pen id")
        nib = _job_number(
            raw_pen.get("nib_mm"),
            f"plot job pen group {group_index} nib_mm",
            positive=True,
        )
        nominal = _job_number(
            raw_pen.get("nominal_nib_mm"),
            f"plot job pen group {group_index} nominal_nib_mm",
            positive=True,
        )
        pen = Pen(
            text_fields["id"],
            text_fields["ink"],
            nib,
            nominal,
            text_fields["colour"],
            text_fields["label"],
            text_fields["calibration_state"],
        )
        strokes: list[Stroke] = []
        for stroke_index, raw_stroke in enumerate(raw_strokes):
            if not isinstance(raw_stroke, dict):
                raise PlotJobError("plot job contains an invalid stroke")
            layer = raw_stroke.get("layer")
            if not isinstance(layer, str) or not layer:
                raise PlotJobError("plot job stroke layer must be a non-empty string")
            raw_points = raw_stroke.get("points_mm")
            if not isinstance(raw_points, list) or len(raw_points) < 2:
                raise PlotJobError(
                    "plot job contains a stroke with fewer than two points"
                )
            points = [_finite_point(point) for point in raw_points]
            source_id = raw_stroke.get("source_stroke_id")
            reversed_value = raw_stroke.get("reversed")
            if not isinstance(source_id, int) or isinstance(source_id, bool):
                raise PlotJobError("plot job source_stroke_id must be an integer")
            if not isinstance(reversed_value, bool):
                raise PlotJobError("plot job stroke reversed flag must be boolean")
            recorded_length = _job_number(
                raw_stroke.get("length_mm"),
                f"plot job stroke {group_index}:{stroke_index} length_mm",
            )
            expected_length = _round(_length(points), 4)
            if recorded_length != expected_length:
                raise PlotJobError(
                    "plot job stroke length disagrees with its point geometry"
                )
            strokes.append(Stroke(layer, pen, points, source_id, reversed_value))
        groups.append((pen, strokes))
    return groups


def verify_plot_job(job: dict[str, Any]) -> None:
    if job.get("schema") != JOB_SCHEMA:
        raise PlotJobError(f"unsupported plot job schema {job.get('schema')!r}")
    expected = job.get("job_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise PlotJobError("plot job has no valid job_sha256")
    actual = _digest(_job_digest_payload(job))
    if actual != expected:
        raise PlotJobError(
            f"plot job digest mismatch: expected {expected}, calculated {actual}"
        )
    source = job.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("name"), str)
        or not source["name"]
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        or not isinstance(source.get("bytes"), int)
        or isinstance(source.get("bytes"), bool)
        or source["bytes"] <= 0
    ):
        raise PlotJobError("plot job source record is invalid")
    page = job.get("page_mm")
    if not isinstance(page, dict):
        raise PlotJobError("plot job page_mm must be an object")
    for key in ("width", "height"):
        _job_number(page.get(key), f"plot job page {key}", positive=True)
    if job.get("order") not in {"document", "merged", "optimised"}:
        raise PlotJobError("plot job has an invalid stroke order")
    groups = _groups_from_job(job)
    raw_machine = job.get("machine")
    if not isinstance(raw_machine, dict):
        raise PlotJobError("plot job machine must be an object")
    try:
        machine = Machine.from_mapping(raw_machine)
    except ValueError as exc:
        raise PlotJobError(f"plot job has an invalid machine model: {exc}") from exc

    moves, recomputed_stats = simulate(groups, machine)
    expected_stats = _serialized_stats(recomputed_stats)
    if job.get("stats") != expected_stats:
        raise PlotJobError(
            "plot job timing or distance statistics disagree with its geometry"
        )
    all_strokes = [stroke for _pen, strokes in groups for stroke in strokes]
    expected_geometry = {
        "curve_flatness_mm": machine.curve_flatness_mm,
        "stroke_count": len(all_strokes),
        "vertex_count": sum(len(stroke.points) for stroke in all_strokes),
        "ink_bounds_mm": _geometry_bounds(all_strokes),
    }
    if job.get("geometry") != expected_geometry:
        raise PlotJobError("plot job geometry summary disagrees with its strokes")
    move_count = job.get("move_count")
    if (
        not isinstance(move_count, int)
        or isinstance(move_count, bool)
        or move_count != len(moves)
    ):
        raise PlotJobError("plot job move_count disagrees with its motion plan")
    safety = job.get("safety")
    if not isinstance(safety, dict) or not isinstance(safety.get("findings"), list):
        raise PlotJobError("plot job safety record is invalid")
    for finding in safety["findings"]:
        if (
            not isinstance(finding, dict)
            or finding.get("severity") not in {"info", "warning", "error", "blocker"}
            or not isinstance(finding.get("code"), str)
            or not finding["code"]
            or not isinstance(finding.get("message"), str)
            or not finding["message"]
        ):
            raise PlotJobError("plot job contains an invalid safety finding")
    expected_allowed = not any(
        finding.get("severity") in {"error", "blocker"}
        for finding in safety["findings"]
    )
    if not isinstance(safety.get("execution_allowed"), bool):
        raise PlotJobError("plot job execution_allowed must be boolean")
    if safety["execution_allowed"] is not expected_allowed:
        raise PlotJobError("plot job execution_allowed disagrees with its findings")
    preflight = job.get("preflight")
    if not isinstance(preflight, dict) or not isinstance(preflight.get("issues"), list):
        raise PlotJobError("plot job preflight record is invalid")
    if (
        preflight.get("source_sha256") != source["sha256"]
        or preflight.get("source_bytes") != source["bytes"]
        or preflight.get("page_mm") != page
    ):
        raise PlotJobError(
            "plot job preflight record disagrees with its source or page"
        )
    copied_preflight_findings = [
        issue
        for issue in preflight["issues"]
        if isinstance(issue, dict) and issue.get("severity") in {"error", "warning"}
    ]
    if any(issue not in safety["findings"] for issue in copied_preflight_findings):
        raise PlotJobError("plot job safety record omits a preflight finding")
    binding = job.get("device_profile")
    if binding is not None and (
        not isinstance(binding, dict)
        or not isinstance(binding.get("id"), str)
        or not binding["id"]
        or not isinstance(binding.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None
    ):
        raise PlotJobError("plot job device_profile binding is invalid")


def validate_job_for_execution(
    job: dict[str, Any], *, allow_review_output: bool = False
) -> None:
    """Allow calibration blockers to be acknowledged, never structural errors."""

    verify_plot_job(job)
    findings: list[dict[str, Any]] = [
        finding
        for finding in job["safety"]["findings"]
        if isinstance(finding, dict) and finding.get("severity") in {"error", "blocker"}
    ]
    groups = _groups_from_job(job)
    if any(not pen.measured for pen, _strokes in groups) and not any(
        finding.get("code") == "unmeasured-pens" for finding in findings
    ):
        findings.append(
            {
                "severity": "blocker",
                "code": "unmeasured-pens",
                "message": "Pen calibration state is not measured.",
            }
        )
    machine = Machine.from_mapping(job["machine"])
    if machine.calibration_state not in {
        "measured",
        "timing-measured",
        "hardware-verified",
    } and not any(
        finding.get("code") == "uncalibrated-machine-timing" for finding in findings
    ):
        findings.append(
            {
                "severity": "blocker",
                "code": "uncalibrated-machine-timing",
                "message": "Machine timing is not calibrated.",
            }
        )
    bounds = job["geometry"]["ink_bounds_mm"]
    page = job["page_mm"]
    tolerance = 1e-6
    if (
        float(bounds["min_x_mm"]) < -tolerance
        or float(bounds["min_y_mm"]) < -tolerance
        or float(bounds["max_x_mm"]) > float(page["width"]) + tolerance
        or float(bounds["max_y_mm"]) > float(page["height"]) + tolerance
    ) and not any(
        finding.get("code") == "geometry-outside-page" for finding in findings
    ):
        findings.append(
            {
                "severity": "error",
                "code": "geometry-outside-page",
                "message": "Stroke ink envelope extends beyond the declared page.",
            }
        )
    hard = [
        finding
        for finding in findings
        if finding.get("severity") == "error"
        or finding.get("code") not in _REVIEW_ONLY_BYPASS_CODES
    ]
    if hard:
        codes = ", ".join(str(finding.get("code", "unknown")) for finding in hard)
        raise PlotJobError(
            f"job has non-bypassable structural safety findings: {codes}"
        )
    if findings and not allow_review_output:
        raise PlotJobError(
            "job is review-only; clear its calibration blockers or explicitly pass "
            "--allow-review-output for a controlled calibration proof"
        )


def read_plot_job(path: Path) -> dict[str, Any]:
    raw = _read_json(path, "plot job")
    if not isinstance(raw, dict):
        raise PlotJobError("plot job root must be an object")
    verify_plot_job(raw)
    return raw


def write_plot_job(path: Path, job: dict[str, Any]) -> None:
    verify_plot_job(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(job, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _commands(raw: Any, label: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise PlotJobError(f"controller {label} must be a list of command strings")
    commands: list[str] = []
    for item in raw:
        command = item.strip()
        if "\n" in command or "\r" in command:
            raise PlotJobError(f"controller {label} commands must contain one line")
        if not command.isascii():
            raise PlotJobError(f"controller {label} commands must contain ASCII only")
        if command:
            commands.append(command)
    return tuple(commands)


def _boolean(raw: Any, label: str, *, default: bool = False) -> bool:
    value = default if raw is None else raw
    if not isinstance(value, bool):
        raise PlotJobError(f"controller {label} must be true or false")
    return value


def _validate_auxiliary_commands(
    header: tuple[str, ...],
    pen_up: tuple[str, ...],
    pen_down: tuple[str, ...],
    footer: tuple[str, ...],
) -> None:
    safe_modal = re.compile(r"(?:G0*(?:17|21|90|94))+\Z", re.I)
    safe_disable = re.compile(r"M0*5\Z", re.I)
    pwm_servo = re.compile(rf"M0*[34]S({_GCODE_NUMBER})\Z", re.I)

    def code(command: str) -> str:
        uncommented = re.sub(r"\([^)]*\)|;.*$", "", command).strip()
        return re.sub(r"\s+", "", uncommented).upper()

    down_codes = {code(command).casefold() for command in pen_down}
    up_codes = {code(command).casefold() for command in pen_up}
    overlap = sorted(down_codes & up_codes)
    if overlap:
        raise PlotJobError(
            "controller pen_up and pen_down commands overlap: " + ", ".join(overlap)
        )
    for label, commands in (("header", header), ("footer", footer)):
        for command in commands:
            executable = code(command)
            if not executable:
                continue
            if executable.casefold() in down_codes:
                raise PlotJobError(
                    f"controller {label} repeats a pen-down command {command!r}"
                )
            if (
                safe_modal.fullmatch(executable) is None
                and safe_disable.fullmatch(executable) is None
            ):
                raise PlotJobError(
                    f"controller {label} command {command!r} contains hidden motion "
                    "or an unsupported side effect; only G17/G21/G90/G94 and M5 "
                    "are allowed"
                )
    for label, commands in (("pen_up", pen_up), ("pen_down", pen_down)):
        for command in commands:
            executable = code(command)
            if not executable:
                continue
            pwm_match = pwm_servo.fullmatch(executable)
            if safe_disable.fullmatch(executable) is not None:
                continue
            if pwm_match is None or not math.isfinite(float(pwm_match.group(1))):
                raise PlotJobError(
                    f"controller {label} command {command!r} contains unmodelled "
                    "motion or timing; plot-job-v1 GRBL pen commands are limited to "
                    "explicit M3/M4 S spindle-PWM values or M5"
                )


@dataclass(frozen=True)
class ControllerProfile:
    id: str
    name: str
    profile_sha256: str
    motion: Machine
    driver: str
    execution_enabled: bool
    serial_baud: int
    work_width_mm: float
    work_height_mm: float
    origin_x_mm: float
    origin_y_mm: float
    invert_x: bool
    invert_y: bool
    page_rotation: str
    coordinate_system: str
    expected_work_offset_mm: tuple[float, float, float]
    header_commands: tuple[str, ...]
    pen_up_commands: tuple[str, ...]
    pen_down_commands: tuple[str, ...]
    footer_commands: tuple[str, ...]
    home_command: str | None
    unlock_command: str | None
    return_home: bool
    calibration_state: str
    expected_grbl_settings: tuple[tuple[str, float], ...]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ControllerProfile":
        if raw.get("schema") != PROFILE_SCHEMA:
            raise PlotJobError(
                f"device profile schema must be {PROFILE_SCHEMA!r}, got {raw.get('schema')!r}"
            )
        controller = raw.get("controller")
        if not isinstance(controller, dict):
            raise PlotJobError("device profile controller must be an object")
        work = raw.get("work_area_mm")
        origin = controller.get("origin_mm", {})
        if not isinstance(work, dict) or not isinstance(origin, dict):
            raise PlotJobError(
                "device profile work_area_mm and origin_mm must be objects"
            )
        rotation_raw = controller.get("page_rotation", "auto")
        if not isinstance(rotation_raw, str):
            raise PlotJobError("controller page_rotation must be a string")
        rotation = rotation_raw
        if rotation not in {"auto", "0", "90", "180", "270"}:
            raise PlotJobError(
                "controller page_rotation must be auto, 0, 90, 180, or 270"
            )
        coordinate_raw = controller.get("coordinate_system", "G54")
        if not isinstance(coordinate_raw, str):
            raise PlotJobError("controller coordinate_system must be a string")
        coordinate_system = coordinate_raw.upper()
        if coordinate_system not in {"G54", "G55", "G56", "G57", "G58", "G59"}:
            raise PlotJobError("controller coordinate_system must be G54 through G59")
        offset_raw = controller.get("expected_work_offset_mm", {})
        if not isinstance(offset_raw, dict):
            raise PlotJobError("controller expected_work_offset_mm must be an object")
        driver_raw = controller.get("driver", "disabled")
        if not isinstance(driver_raw, str):
            raise PlotJobError("controller driver must be a string")
        driver = driver_raw
        if driver not in {"disabled", "grbl"}:
            raise PlotJobError(f"unsupported controller driver {driver!r}")
        home = controller.get("home_command")
        unlock = controller.get("unlock_command")
        for label, command in (("home_command", home), ("unlock_command", unlock)):
            if command is not None and (
                not isinstance(command, str)
                or "\n" in command
                or "\r" in command
                or not command.isascii()
            ):
                raise PlotJobError(
                    f"controller {label} must be one command string or null"
                )
        header_commands = _commands(
            controller.get("header_commands"), "header_commands"
        )
        pen_up_commands = _commands(
            controller.get("pen_up_commands"), "pen_up_commands"
        )
        pen_down_commands = _commands(
            controller.get("pen_down_commands"), "pen_down_commands"
        )
        footer_commands = _commands(
            controller.get("footer_commands"), "footer_commands"
        )
        expected_raw = controller.get("expected_settings", {})
        if not isinstance(expected_raw, dict):
            raise PlotJobError("controller expected_settings must be an object")
        expected_settings: list[tuple[str, float]] = []
        for setting, raw_value in expected_raw.items():
            if not isinstance(setting, str) or re.fullmatch(r"\$\d+", setting) is None:
                raise PlotJobError(
                    "controller expected_settings keys must look like '$120'"
                )
            if isinstance(raw_value, bool):
                raise PlotJobError(
                    f"controller expected setting {setting} must be numeric"
                )
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise PlotJobError(
                    f"controller expected setting {setting} must be numeric"
                ) from exc
            if not math.isfinite(value):
                raise PlotJobError(
                    f"controller expected setting {setting} must be finite"
                )
            expected_settings.append((setting, value))
        _validate_auxiliary_commands(
            header_commands,
            pen_up_commands,
            pen_down_commands,
            footer_commands,
        )
        baud = controller.get("serial_baud", 115200)
        if not isinstance(baud, int) or isinstance(baud, bool):
            raise PlotJobError("controller serial_baud must be an integer")
        profile_id = raw.get("id")
        profile_name = raw.get("name")
        calibration_state = raw.get("calibration_state")
        if (
            not isinstance(profile_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", profile_id) is None
        ):
            raise PlotJobError(
                "device profile id must contain only letters, digits, dot, "
                "underscore, or hyphen"
            )
        if not isinstance(profile_name, str) or not profile_name:
            raise PlotJobError("device profile name must be a non-empty string")
        if not isinstance(calibration_state, str) or not calibration_state:
            raise PlotJobError(
                "device profile calibration_state must be a non-empty string"
            )
        numeric_inputs = [
            work.get("width", 0),
            work.get("height", 0),
            origin.get("x", 0),
            origin.get("y", 0),
            *(offset_raw.get(axis, 0.0) for axis in ("x", "y", "z")),
        ]
        if any(isinstance(value, bool) for value in numeric_inputs):
            raise PlotJobError("device profile numeric values cannot be booleans")
        try:
            motion = Machine.from_mapping(raw)
            work_width = float(work.get("width", 0))
            work_height = float(work.get("height", 0))
            origin_x = float(origin.get("x", 0))
            origin_y = float(origin.get("y", 0))
            work_offset = (
                float(offset_raw.get("x", 0.0)),
                float(offset_raw.get("y", 0.0)),
                float(offset_raw.get("z", 0.0)),
            )
        except (TypeError, ValueError) as exc:
            raise PlotJobError(
                f"device profile has invalid numeric values: {exc}"
            ) from exc
        profile = cls(
            id=profile_id,
            name=profile_name,
            profile_sha256=_digest(raw),
            motion=motion,
            driver=driver,
            execution_enabled=_boolean(
                controller.get("execution_enabled"), "execution_enabled"
            ),
            serial_baud=baud,
            work_width_mm=work_width,
            work_height_mm=work_height,
            origin_x_mm=origin_x,
            origin_y_mm=origin_y,
            invert_x=_boolean(controller.get("invert_x"), "invert_x"),
            invert_y=_boolean(controller.get("invert_y"), "invert_y"),
            page_rotation=rotation,
            coordinate_system=coordinate_system,
            expected_work_offset_mm=work_offset,
            header_commands=header_commands,
            pen_up_commands=pen_up_commands,
            pen_down_commands=pen_down_commands,
            footer_commands=footer_commands,
            home_command=home.strip()
            if isinstance(home, str) and home.strip()
            else None,
            unlock_command=unlock.strip()
            if isinstance(unlock, str) and unlock.strip()
            else None,
            return_home=_boolean(controller.get("return_home"), "return_home"),
            calibration_state=calibration_state,
            expected_grbl_settings=tuple(sorted(expected_settings)),
        )
        if profile.serial_baud <= 0:
            raise PlotJobError("controller serial_baud must be positive")
        for label, value in (
            ("work width", profile.work_width_mm),
            ("work height", profile.work_height_mm),
            ("origin x", profile.origin_x_mm),
            ("origin y", profile.origin_y_mm),
        ):
            work_dimension = label.startswith("work")
            if (
                not math.isfinite(value)
                or (work_dimension and value <= 0)
                or (not work_dimension and value < 0)
            ):
                relation = "positive" if work_dimension else "non-negative"
                raise PlotJobError(f"controller {label} must be {relation} and finite")
        if not all(math.isfinite(value) for value in profile.expected_work_offset_mm):
            raise PlotJobError(
                "controller expected work-coordinate offsets must be finite"
            )
        if not math.isclose(
            profile.motion.work_width_mm,
            profile.work_width_mm,
            abs_tol=1e-9,
            rel_tol=0.0,
        ) or not math.isclose(
            profile.motion.work_height_mm,
            profile.work_height_mm,
            abs_tol=1e-9,
            rel_tol=0.0,
        ):
            raise PlotJobError(
                "device profile motion work area disagrees with work_area_mm"
            )
        if profile.home_command not in {None, "$H"}:
            raise PlotJobError("GRBL home_command must be '$H' or null")
        if profile.unlock_command not in {None, "$X"}:
            raise PlotJobError("GRBL unlock_command must be '$X' or null")
        settings = dict(profile.expected_grbl_settings)
        if (
            profile.driver == "grbl"
            and profile.execution_enabled
            and profile.calibration_state == "hardware-verified"
        ):
            if profile.motion.motion_model != "grbl-cartesian":
                raise PlotJobError(
                    "hardware-verified GRBL profile must use the "
                    "grbl-cartesian motion model"
                )
            if profile.unlock_command != "$X":
                raise PlotJobError(
                    "hardware-verified GRBL execution requires unlock_command '$X' "
                    "for the check-mode reset sequence"
                )
            if "expected_work_offset_mm" not in controller:
                raise PlotJobError(
                    "hardware-verified GRBL profile must declare "
                    "expected_work_offset_mm"
                )
            required = {
                "$3",
                "$11",
                "$100",
                "$101",
                "$110",
                "$111",
                "$120",
                "$121",
                "$130",
                "$131",
            }
            pen_commands = (*profile.pen_up_commands, *profile.pen_down_commands)
            if any(
                re.search(r"(?<![A-Z0-9])M\s*0*[34](?!\d)", command, re.I)
                and re.search(r"(?<![A-Z])S\s*[-+]?\d", command, re.I)
                for command in pen_commands
            ):
                required.update({"$30", "$31", "$32"})
            missing = sorted(required - settings.keys())
            if missing:
                raise PlotJobError(
                    "hardware-verified GRBL profile lacks expected setting(s): "
                    + ", ".join(missing)
                )
            spindle_values = [
                float(match.group(1))
                for command in pen_commands
                for match in re.finditer(
                    rf"(?<![A-Z])S\s*({_GCODE_NUMBER})",
                    command,
                    re.I,
                )
            ]
            if spindle_values and any(
                value < settings["$31"] - 1e-6 or value > settings["$30"] + 1e-6
                for value in spindle_values
            ):
                raise PlotJobError(
                    "GRBL pen command S value lies outside expected $31/$30 range"
                )
            if spindle_values and abs(settings["$32"]) > 1e-9:
                raise PlotJobError(
                    "GRBL $32 laser mode must be disabled when spindle PWM controls the pen"
                )
            maximum_feed = (
                max(
                    profile.motion.pen_up_speed_mm_s,
                    profile.motion.pen_down_speed_mm_s,
                )
                * 60.0
            )
            if (
                settings["$110"] + 1e-6 < maximum_feed
                or settings["$111"] + 1e-6 < maximum_feed
            ):
                raise PlotJobError(
                    "GRBL $110/$111 maximum rates are below the modeled XY feed"
                )
            if settings["$100"] <= 0 or settings["$101"] <= 0:
                raise PlotJobError("GRBL $100/$101 steps per mm must be positive")
            if not settings["$3"].is_integer() or not 0 <= settings["$3"] <= 7:
                raise PlotJobError(
                    "GRBL $3 direction mask must be an integer from 0 to 7"
                )
            if (
                settings["$130"] + 1e-6 < profile.work_width_mm
                or settings["$131"] + 1e-6 < profile.work_height_mm
            ):
                raise PlotJobError(
                    "GRBL $130/$131 maximum travel is below the modeled work area"
                )
            for setting in ("$120", "$121"):
                if not math.isclose(
                    settings[setting],
                    profile.motion.acceleration_mm_s2,
                    rel_tol=1e-4,
                    abs_tol=1e-4,
                ):
                    raise PlotJobError(
                        f"GRBL {setting} must match the modeled acceleration"
                    )
            if not math.isclose(
                settings["$11"],
                profile.motion.cornering_tolerance_mm,
                rel_tol=1e-4,
                abs_tol=1e-4,
            ):
                raise PlotJobError(
                    "GRBL $11 must match the modeled cornering tolerance"
                )
        return profile

    def binding(self) -> dict[str, str]:
        return {"id": self.id, "sha256": self.profile_sha256}

    @classmethod
    def from_json(cls, path: Path) -> "ControllerProfile":
        raw = _read_json(path, "device profile")
        if not isinstance(raw, dict):
            raise PlotJobError("device profile root must be an object")
        return cls.from_mapping(raw)


def load_device_profile(
    path: Path,
) -> tuple[dict[str, Any], Machine, ControllerProfile]:
    raw = _read_json(path, "device profile")
    if not isinstance(raw, dict):
        raise PlotJobError("device profile root must be an object")
    profile = ControllerProfile.from_mapping(raw)
    return raw, profile.motion, profile


def _rotation_for(job: dict[str, Any], profile: ControllerProfile) -> int:
    page = job["page_mm"]
    width, height = float(page["width"]), float(page["height"])
    requested = profile.page_rotation
    candidates = (0, 90) if requested == "auto" else (int(requested),)
    for rotation in candidates:
        out_width, out_height = (
            (width, height) if rotation in {0, 180} else (height, width)
        )
        if (
            profile.origin_x_mm >= -1e-9
            and profile.origin_y_mm >= -1e-9
            and profile.origin_x_mm + out_width <= profile.work_width_mm + 1e-9
            and profile.origin_y_mm + out_height <= profile.work_height_mm + 1e-9
        ):
            return rotation
    raise PlotJobError(
        f"{width:g} x {height:g} mm page does not fit profile {profile.id!r} "
        f"within {profile.work_width_mm:g} x {profile.work_height_mm:g} mm"
    )


def _transform_point(
    point: Iterable[Any],
    page: dict[str, Any],
    profile: ControllerProfile,
    rotation: int,
) -> tuple[float, float]:
    x, y = _finite_point(point)
    width, height = float(page["width"]), float(page["height"])
    if rotation == 0:
        tx, ty, out_width, out_height = x, y, width, height
    elif rotation == 90:
        tx, ty, out_width, out_height = height - y, x, height, width
    elif rotation == 180:
        tx, ty, out_width, out_height = width - x, height - y, width, height
    else:
        tx, ty, out_width, out_height = y, width - x, height, width
    if profile.invert_x:
        tx = out_width - tx
    if profile.invert_y:
        ty = out_height - ty
    return profile.origin_x_mm + tx, profile.origin_y_mm + ty


def _gcode_number(value: float) -> str:
    # Plot-job coordinates are canonical at six decimal places.  Retaining all
    # six here keeps export geometry identical to the verified simulation.
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _safe_slug(value: str) -> str:
    slug = _SAFE_ID.sub("-", value.casefold()).strip("-.")
    return slug or "pen"


def _ascii_comment(value: Any) -> str:
    ascii_value = str(value).encode("ascii", "replace").decode("ascii")
    return "".join(
        character if 32 <= ord(character) <= 126 else " " for character in ascii_value
    )


def compile_gcode_files(
    job: dict[str, Any],
    profile: ControllerProfile,
    *,
    bounds_only: bool = False,
    allow_review_output: bool = False,
) -> dict[str, str]:
    """Create deterministic, per-pen GRBL programs from one verified plot job."""

    validate_job_for_execution(
        job,
        allow_review_output=allow_review_output or bounds_only,
    )
    binding = job.get("device_profile")
    if binding is None:
        raise PlotJobError(
            "plot job is simulation-only and has no bound device profile; recompile "
            "the SVG with --profile"
        )
    if binding != profile.binding():
        raise PlotJobError(
            "plot job device profile does not match the selected controller profile"
        )
    if job.get("machine") != profile.motion.as_dict():
        raise PlotJobError(
            "plot job motion model disagrees with its bound device profile"
        )
    if profile.driver != "grbl":
        raise PlotJobError(f"profile {profile.id!r} is not a GRBL controller profile")
    if profile.return_home:
        raise PlotJobError(
            "implicit per-pen return_home is not part of plot-job-v1; keep it false "
            "so executed travel remains identical to the simulation"
        )
    if not profile.pen_up_commands:
        raise PlotJobError("GRBL profile has no explicit pen_up_commands")
    if not bounds_only and not profile.pen_down_commands:
        raise PlotJobError("GRBL profile has no explicit pen_down_commands")
    machine = Machine.from_mapping(job["machine"])
    rotation = _rotation_for(job, profile)
    page = job["page_mm"]

    def move_line(point: Iterable[Any], speed: float) -> str:
        x, y = _transform_point(point, page, profile, rotation)
        if (
            x < -1e-6
            or y < -1e-6
            or x > profile.work_width_mm + 1e-6
            or y > profile.work_height_mm + 1e-6
        ):
            raise PlotJobError(f"transformed point ({x:g}, {y:g}) leaves work area")
        return (
            f"G1 X{_gcode_number(x)} Y{_gcode_number(y)} F{_gcode_number(speed * 60)}"
        )

    identity = [
        f"; {JOB_SCHEMA}",
        f"; job_sha256={job['job_sha256']}",
        f"; source_sha256={job['source']['sha256']}",
        f"; profile={_ascii_comment(profile.id)}",
        f"; page_rotation={rotation}",
    ]
    standard_modes = [
        "G21",
        "G90",
        "G94",
        profile.coordinate_system,
    ]
    common = [
        *identity,
        *standard_modes,
        *profile.header_commands,
        *profile.pen_up_commands,
    ]
    dwell_up = (
        f"G4 P{_gcode_number(machine.pen_lift_s)}" if machine.pen_lift_s else None
    )
    dwell_down = (
        f"G4 P{_gcode_number(machine.pen_lower_s)}" if machine.pen_lower_s else None
    )
    files: dict[str, str] = {}

    if bounds_only:
        corners = [
            [0.0, 0.0],
            [float(page["width"]), 0.0],
            [float(page["width"]), float(page["height"])],
            [0.0, float(page["height"])],
            [0.0, 0.0],
        ]
        # Bounds mode deliberately excludes arbitrary header/footer commands.
        # Its only profile-defined behavior is the validated pen-up sequence.
        lines = [
            *identity,
            *standard_modes,
            *profile.pen_up_commands,
            "; PEN MUST REMAIN UP - page bounds preview",
        ]
        if dwell_up:
            lines.append(dwell_up)
        lines.extend(move_line(point, machine.pen_up_speed_mm_s) for point in corners)
        files["00-bounds-preview.gcode"] = "\n".join(lines) + "\n"
        return files

    for sequence, group in enumerate(job["pen_groups"], start=1):
        pen = group["pen"]
        lines = [
            *common,
            f"; load_pen={_ascii_comment(pen['id'])}",
            f"; ink={_ascii_comment(pen['ink'])} nominal_nib_mm={pen['nominal_nib_mm']}",
        ]
        if dwell_up:
            lines.append(dwell_up)
        for stroke in group["strokes"]:
            points = stroke["points_mm"]
            lines.append(move_line(points[0], machine.pen_up_speed_mm_s))
            lines.extend(profile.pen_down_commands)
            if dwell_down:
                lines.append(dwell_down)
            for point in points[1:]:
                lines.append(move_line(point, machine.pen_down_speed_mm_s))
            lines.extend(profile.pen_up_commands)
            if dwell_up:
                lines.append(dwell_up)
        lines.extend(profile.footer_commands)
        filename = f"{sequence:02d}-{_safe_slug(str(pen['id']))}.gcode"
        files[filename] = "\n".join(lines) + "\n"
    return files


def write_gcode_files(directory: Path, files: dict[str, str]) -> None:
    if directory.exists():
        if not directory.is_dir():
            raise PlotJobError(f"G-code output path is not a directory: {directory}")
        if any(directory.iterdir()):
            raise PlotJobError(
                f"G-code output directory is not empty: {directory}; use a fresh "
                "directory so stale pen programs cannot be mixed with this job"
            )
    else:
        directory.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (directory / name).write_text(content, encoding="ascii")


class GrblStreamer:
    """Small acknowledged-line GRBL transport with explicit emergency hold."""

    def __init__(self, port: str, baud: int, timeout_s: float = 2.0):
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - hardware environment
            raise PlotJobError(
                "serial execution requires pyserial; install the plotter optional dependency"
            ) from exc
        self._serial_module = serial
        self.port = port
        self.baud = baud
        self.timeout_s = timeout_s
        self.connection: Any = None

    def __enter__(self) -> "GrblStreamer":  # pragma: no cover - hardware environment
        try:
            self.connection = self._serial_module.Serial(
                self.port,
                self.baud,
                timeout=self.timeout_s,
                write_timeout=self.timeout_s,
            )
            self.connection.write(b"\r\n\r\n")
            time.sleep(2.0)
            self.connection.reset_input_buffer()
        except Exception as exc:
            raise PlotJobError(
                f"could not open or initialise GRBL serial port {self.port!r}: {exc}"
            ) from exc
        return self

    def __exit__(
        self, *_exc: object
    ) -> None:  # pragma: no cover - hardware environment
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass

    def emergency_hold(self) -> None:  # pragma: no cover - hardware environment
        if self.connection is not None:
            try:
                self.connection.write(b"!")
                self.connection.flush()
            except Exception:
                # The link may already be gone.  Do not hide the original
                # failure while attempting the best-effort realtime hold.
                pass

    def command(
        self, line: str, *, timeout_s: float | None = None
    ) -> str:  # pragma: no cover - hardware environment
        if self.connection is None:
            raise PlotJobError("GRBL serial connection is not open")
        timeout = self.timeout_s if timeout_s is None else timeout_s
        if not math.isfinite(timeout) or timeout <= 0:
            raise PlotJobError("GRBL command timeout must be positive and finite")
        try:
            encoded = line.strip().encode("ascii") + b"\n"
            self.connection.write(encoded)
            self.connection.flush()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                response = self.connection.readline().decode("ascii", "replace").strip()
                if response == "ok":
                    return response
                if response.startswith("error") or response.startswith("ALARM"):
                    raise PlotJobError(f"GRBL rejected {line!r}: {response}")
        except PlotJobError:
            raise
        except Exception as exc:
            raise PlotJobError(f"GRBL serial I/O failed for {line!r}: {exc}") from exc
        raise PlotJobError(f"timed out waiting for GRBL acknowledgement of {line!r}")

    def read_settings(
        self,
    ) -> dict[str, float]:  # pragma: no cover - hardware environment
        """Read GRBL's live ``$$`` values instead of trusting a profile file."""

        if self.connection is None:
            raise PlotJobError("GRBL serial connection is not open")
        try:
            self.connection.write(b"$$\n")
            self.connection.flush()
            deadline = time.monotonic() + max(5.0, self.timeout_s * 5.0)
            settings: dict[str, float] = {}
            pattern = re.compile(rf"^(\$\d+)=({_GCODE_NUMBER})")
            while time.monotonic() < deadline:
                response = self.connection.readline().decode("ascii", "replace").strip()
                if not response:
                    continue
                if response == "ok":
                    if not settings:
                        raise PlotJobError("GRBL returned no settings for '$$'")
                    return settings
                if response.startswith("error") or response.startswith("ALARM"):
                    raise PlotJobError(f"GRBL rejected '$$': {response}")
                match = pattern.match(response)
                if match:
                    settings[match.group(1)] = float(match.group(2))
        except PlotJobError:
            raise
        except Exception as exc:
            raise PlotJobError(
                f"GRBL serial I/O failed while reading '$$': {exc}"
            ) from exc
        raise PlotJobError("timed out while reading GRBL settings")

    def verify_settings(
        self, expected: Iterable[tuple[str, float]]
    ) -> None:  # pragma: no cover - hardware environment
        actual = self.read_settings()
        for setting, wanted in expected:
            if setting not in actual:
                raise PlotJobError(f"GRBL did not report required setting {setting}")
            tolerance = max(1e-4, abs(wanted) * 1e-4)
            if not math.isclose(
                actual[setting], wanted, abs_tol=tolerance, rel_tol=0.0
            ):
                raise PlotJobError(
                    f"GRBL {setting} is {actual[setting]:g}, expected {wanted:g}; "
                    "firmware settings do not match the simulated profile"
                )

    def read_coordinate_offsets(
        self,
    ) -> dict[
        str, tuple[float, float, float]
    ]:  # pragma: no cover - hardware environment
        """Read persistent G54–G59 and temporary G92 offsets from ``$#``."""

        if self.connection is None:
            raise PlotJobError("GRBL serial connection is not open")
        try:
            self.connection.write(b"$#\n")
            self.connection.flush()
            deadline = time.monotonic() + max(5.0, self.timeout_s * 5.0)
            offsets: dict[str, tuple[float, float, float]] = {}
            pattern = re.compile(r"^\[([A-Z0-9.]+):([^\]]+)\]", re.I)
            while time.monotonic() < deadline:
                response = self.connection.readline().decode("ascii", "replace").strip()
                if not response:
                    continue
                if response == "ok":
                    if not offsets:
                        raise PlotJobError("GRBL returned no coordinate data for '$#'")
                    return offsets
                if response.startswith("error") or response.startswith("ALARM"):
                    raise PlotJobError(f"GRBL rejected '$#': {response}")
                match = pattern.match(response)
                if not match:
                    continue
                try:
                    values = [float(value) for value in match.group(2).split(",")[:3]]
                except ValueError:
                    continue
                if len(values) == 3 and all(math.isfinite(value) for value in values):
                    offsets[match.group(1).upper()] = (
                        values[0],
                        values[1],
                        values[2],
                    )
        except PlotJobError:
            raise
        except Exception as exc:
            raise PlotJobError(
                f"GRBL serial I/O failed while reading '$#': {exc}"
            ) from exc
        raise PlotJobError("timed out while reading GRBL coordinate offsets")

    def verify_coordinate_offsets(
        self,
        coordinate_system: str,
        expected: tuple[float, float, float],
    ) -> None:  # pragma: no cover - hardware environment
        actual = self.read_coordinate_offsets()
        if coordinate_system not in actual:
            raise PlotJobError(
                f"GRBL did not report required coordinate system {coordinate_system}"
            )
        for axis, got, wanted in zip("XYZ", actual[coordinate_system], expected):
            if not math.isclose(got, wanted, abs_tol=1e-4, rel_tol=0.0):
                raise PlotJobError(
                    f"GRBL {coordinate_system} {axis} offset is {got:g}, expected "
                    f"{wanted:g}; work coordinates do not match the bound profile"
                )
        if "G92" not in actual:
            raise PlotJobError("GRBL did not report the temporary G92 offset")
        g92 = actual["G92"]
        if any(abs(value) > 1e-4 for value in g92):
            raise PlotJobError(
                "GRBL has an active G92 temporary offset; clear it and re-run the "
                "bounds proof"
            )

    def wait_until_idle(
        self,
        timeout_s: float,
        *,
        poll_interval_s: float = 0.1,
    ) -> None:  # pragma: no cover - hardware environment
        """Wait for physical completion; GRBL ``ok`` only means accepted."""

        if self.connection is None:
            raise PlotJobError("GRBL serial connection is not open")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise PlotJobError("GRBL idle timeout must be positive and finite")
        try:
            deadline = time.monotonic() + timeout_s
            last_state = "no status"
            while time.monotonic() < deadline:
                self.connection.write(b"?")
                self.connection.flush()
                query_deadline = min(deadline, time.monotonic() + self.timeout_s)
                while time.monotonic() < query_deadline:
                    response = (
                        self.connection.readline().decode("ascii", "replace").strip()
                    )
                    if not response:
                        continue
                    if response.startswith("ALARM"):
                        raise PlotJobError(
                            f"GRBL alarm while waiting for idle: {response}"
                        )
                    if not response.startswith("<"):
                        continue
                    last_state = response[1:].split("|", 1)[0].split(">", 1)[0]
                    if last_state.casefold() == "idle":
                        return
                    if last_state.casefold() in {"alarm", "door"}:
                        raise PlotJobError(
                            "GRBL entered unsafe state while waiting for idle: "
                            f"{last_state}"
                        )
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0 and poll_interval_s > 0:
                    time.sleep(min(poll_interval_s, remaining))
        except PlotJobError:
            raise
        except Exception as exc:
            raise PlotJobError(
                f"GRBL serial I/O failed while waiting for Idle: {exc}"
            ) from exc
        raise PlotJobError(
            f"timed out waiting for GRBL Idle state (last state: {last_state})"
        )

    @staticmethod
    def _program_commands(program: str) -> list[str]:
        return [
            line.strip()
            for line in program.splitlines()
            if line.strip() and not line.lstrip().startswith(";")
        ]

    def check_program(
        self,
        program: str,
        *,
        acknowledgement_timeout_s: float = 30.0,
    ) -> None:  # pragma: no cover - hardware environment
        """Ask GRBL to parse every line in check mode without moving motors."""

        commands = self._program_commands(program)
        entered = False
        failure: PlotJobError | None = None
        try:
            self.command("$C", timeout_s=acknowledgement_timeout_s)
            entered = True
            for command in commands:
                self.command(command, timeout_s=acknowledgement_timeout_s)
        except PlotJobError as exc:
            failure = exc
        finally:
            if entered:
                try:
                    self.command("$C", timeout_s=acknowledgement_timeout_s)
                except PlotJobError as exc:
                    failure = failure or exc
        if failure is not None:
            raise PlotJobError(
                f"GRBL check-mode validation failed: {failure}"
            ) from failure

    def stream(
        self,
        program: str,
        progress: Callable[[int, int], None] | None = None,
        *,
        idle_timeout_s: float,
    ) -> None:  # pragma: no cover - hardware environment
        commands = self._program_commands(program)
        total = len(commands)
        try:
            acknowledgement_timeout = min(
                idle_timeout_s,
                max(30.0, self.timeout_s * 5.0),
            )
            if self.connection is None:
                raise PlotJobError("GRBL serial connection is not open")
            # Official GRBL character-counting flow control keeps its 128-byte
            # receive buffer busy without overflowing it, so short flattened
            # curve segments do not starve the 16-block look-ahead planner.
            encoded = [command.encode("ascii") + b"\n" for command in commands]
            if any(len(line) >= 128 for line in encoded):
                raise PlotJobError("G-code line exceeds GRBL's serial receive buffer")
            pending: list[int] = []
            sent = 0
            acknowledged = 0
            buffered = 0
            deadline = time.monotonic() + acknowledgement_timeout
            while acknowledged < total:
                wrote = False
                while sent < total and buffered + len(encoded[sent]) <= 127:
                    self.connection.write(encoded[sent])
                    pending.append(len(encoded[sent]))
                    buffered += len(encoded[sent])
                    sent += 1
                    wrote = True
                if wrote:
                    self.connection.flush()
                response = self.connection.readline().decode("ascii", "replace").strip()
                if response == "ok":
                    if not pending:
                        raise PlotJobError("GRBL returned an unmatched acknowledgement")
                    buffered -= pending.pop(0)
                    acknowledged += 1
                    deadline = time.monotonic() + acknowledgement_timeout
                    if progress:
                        progress(acknowledged, total)
                    continue
                if response.startswith("error") or response.startswith("ALARM"):
                    raise PlotJobError(
                        f"GRBL rejected buffered line {acknowledged + 1}: {response}"
                    )
                if time.monotonic() >= deadline:
                    raise PlotJobError(
                        "timed out waiting for GRBL buffered acknowledgements"
                    )
            self.wait_until_idle(idle_timeout_s)
        except KeyboardInterrupt:
            self.emergency_hold()
            raise PlotJobError(
                "operator interrupted the job; GRBL feed hold was sent. "
                "Lift the pen, reset the controller, and re-home before restart."
            ) from None
        except PlotJobError:
            self.emergency_hold()
            raise
        except Exception as exc:
            self.emergency_hold()
            raise PlotJobError(f"GRBL serial streaming failed: {exc}") from exc


def _format_seconds(seconds: float) -> str:
    value = max(0, int(round(seconds)))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _print_job(job: dict[str, Any]) -> None:
    stats = job["stats"]
    safety = job["safety"]
    print(
        f"{job['title']} — {job['page_mm']['width']:g} x {job['page_mm']['height']:g} mm"
    )
    print(f"  job       {job['job_sha256']}")
    print(f"  source    {job['source']['sha256']}  {job['source']['name']}")
    print(
        f"  estimate  {_format_seconds(stats['total_seconds'])} "
        f"({_format_seconds(stats['total_low_seconds'])}–"
        f"{_format_seconds(stats['total_high_seconds'])})"
    )
    print(
        f"  motion    {stats['pen_down_mm'] / 1000:.2f} m down, "
        f"{stats['pen_up_mm'] / 1000:.2f} m up, {stats['pen_lifts']} lifts"
    )
    print(
        f"  program   {job['geometry']['stroke_count']} strokes, "
        f"{job['geometry']['vertex_count']} vertices, "
        f"{len(job['pen_groups'])} pen loads"
    )
    print(f"  execute   {'allowed' if safety['execution_allowed'] else 'BLOCKED'}")
    for finding in safety["findings"]:
        print(
            f"    {finding['severity'].upper():7} {finding['code']}: {finding['message']}"
        )


def _fit_timing_profile(
    path: Path,
    output: Path,
    observations: list[str],
) -> None:
    """Fit only motor motion, keeping measured servo and operator time fixed."""

    if not observations:
        raise PlotJobError("at least one timed plot observation is required")
    raw = _read_json(path, "device profile")
    if not isinstance(raw, dict):
        raise PlotJobError("device profile root must be an object")
    profile = ControllerProfile.from_mapping(raw)
    ratios: list[float] = []
    records: list[dict[str, Any]] = []
    residual_inputs: list[tuple[float, float, float]] = []
    for observation in observations:
        try:
            job_text, actual_text = observation.rsplit(":", 1)
            actual = float(actual_text)
        except (TypeError, ValueError) as exc:
            raise PlotJobError(
                f"invalid observation {observation!r}; use JOB.plotjob.json:ACTUAL_SECONDS"
            ) from exc
        if not math.isfinite(actual) or actual <= 0:
            raise PlotJobError("observed plot durations must be positive and finite")
        job = read_plot_job(Path(job_text))
        if job.get("device_profile") != profile.binding():
            raise PlotJobError(
                f"timing job {job_text} is not bound to device profile {profile.id!r}"
            )
        stats = job.get("stats", {})
        try:
            predicted = float(stats["total_seconds"])
            kinematic = float(stats["kinematic_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PlotJobError(
                f"timing job {job_text} lacks the plot-job-v1 timing breakdown"
            ) from exc
        fixed = predicted - kinematic
        if kinematic <= 0 or fixed < -1e-6 or actual <= fixed:
            raise PlotJobError(
                f"timing observation {job_text} cannot fit positive motor motion"
            )
        ratio = (actual - fixed) / kinematic
        if not math.isfinite(ratio) or ratio <= 0:
            raise PlotJobError(f"timing observation {job_text} has an invalid ratio")
        ratios.append(ratio)
        residual_inputs.append((actual, fixed, kinematic))
        records.append(
            {
                "job_sha256": job["job_sha256"],
                "actual_seconds": round(actual, 6),
                "predicted_seconds": round(predicted, 6),
                "fixed_seconds": round(fixed, 6),
                "kinematic_seconds": round(kinematic, 6),
                "motor_scale_ratio": round(ratio, 8),
            }
        )
    scale = statistics.median(ratios)
    deviations = [
        abs((fixed + kinematic * scale) / actual - 1.0)
        for actual, fixed, kinematic in residual_inputs
    ]
    uncertainty = max(0.02, max(deviations, default=0.0))
    motion = raw.setdefault("motion", {})
    if not isinstance(motion, dict):
        raise PlotJobError("device profile motion must be an object")
    previous = float(motion.get("timing_scale", 1.0))
    motion["timing_scale"] = round(previous * scale, 8)
    motion["timing_uncertainty_fraction"] = round(min(uncertainty, 1.0), 8)
    motion["calibration_state"] = "timing-measured"
    raw["timing_calibration"] = {
        "method": "median motor-only scale with servo/operator time held fixed",
        "sample_count": len(ratios),
        "observations": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {output}: timing scale {motion['timing_scale']}, "
        f"uncertainty ±{100 * motion['timing_uncertainty_fraction']:.1f}%"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser("compile", help="compile SVG to a plot job")
    compile_parser.add_argument("svg", type=Path)
    compile_parser.add_argument("--profile", type=Path)
    compile_parser.add_argument("--out", required=True, type=Path)
    compile_parser.add_argument(
        "--order", choices=("document", "merged", "optimised"), default="optimised"
    )
    compile_parser.add_argument("--allow-unsupported-svg", action="store_true")

    inspect_parser = subparsers.add_parser(
        "inspect", help="verify and report a plot job"
    )
    inspect_parser.add_argument("job", type=Path)

    export_parser = subparsers.add_parser(
        "export-gcode", help="write per-pen GRBL files"
    )
    export_parser.add_argument("job", type=Path)
    export_parser.add_argument("--profile", required=True, type=Path)
    export_parser.add_argument("--out-dir", required=True, type=Path)
    export_parser.add_argument("--bounds-only", action="store_true")
    export_parser.add_argument("--allow-review-output", action="store_true")

    run_parser = subparsers.add_parser("run", help="stream a verified job to GRBL")
    run_parser.add_argument("job", type=Path)
    run_parser.add_argument("--profile", required=True, type=Path)
    run_parser.add_argument("--port", required=True)
    run_parser.add_argument("--execute", action="store_true")
    run_parser.add_argument("--confirm-job-sha")
    run_parser.add_argument("--allow-review-output", action="store_true")
    run_parser.add_argument("--skip-homing", action="store_true")
    run_parser.add_argument(
        "--bounds-only",
        action="store_true",
        help="run only the pen-up page outline; calibration blockers are allowed",
    )

    calibrate_parser = subparsers.add_parser(
        "calibrate-time", help="fit profile motor timing from observed plot jobs"
    )
    calibrate_parser.add_argument("profile", type=Path)
    calibrate_parser.add_argument(
        "--observation",
        action="append",
        required=True,
        metavar="JOB:ACTUAL_SECONDS",
    )
    calibrate_parser.add_argument("--out", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "compile":
            profile_binding = None
            if args.profile:
                _raw, machine, controller = load_device_profile(args.profile)
                profile_binding = controller.binding()
            else:
                machine = Machine()
            job = compile_plot_job(
                args.svg,
                machine,
                order=args.order,
                strict_svg=not args.allow_unsupported_svg,
                profile_binding=profile_binding,
            )
            write_plot_job(args.out, job)
            _print_job(job)
            print(f"  wrote     {args.out}")
            return 0
        if args.command == "inspect":
            _print_job(read_plot_job(args.job))
            return 0
        if args.command == "export-gcode":
            job = read_plot_job(args.job)
            _raw, _machine, profile = load_device_profile(args.profile)
            files = compile_gcode_files(
                job,
                profile,
                bounds_only=args.bounds_only,
                allow_review_output=args.allow_review_output,
            )
            write_gcode_files(args.out_dir, files)
            print(f"wrote {len(files)} program(s) to {args.out_dir}")
            return 0
        if args.command == "calibrate-time":
            _fit_timing_profile(args.profile, args.out, args.observation)
            return 0

        job = read_plot_job(args.job)
        _raw, _machine, profile = load_device_profile(args.profile)
        if not args.execute:
            raise PlotJobError(
                "hardware streaming requires the explicit --execute flag"
            )
        if args.confirm_job_sha != job["job_sha256"]:
            raise PlotJobError(
                "--confirm-job-sha must exactly match the verified job digest"
            )
        if not profile.execution_enabled:
            raise PlotJobError("device profile execution_enabled is false")
        if profile.calibration_state != "hardware-verified":
            raise PlotJobError(
                "device profile calibration_state is not hardware-verified"
            )
        programs = compile_gcode_files(
            job,
            profile,
            bounds_only=args.bounds_only,
            allow_review_output=args.allow_review_output,
        )
        idle_timeout_s = max(60.0, float(job["stats"]["total_high_seconds"]) + 60.0)
        with GrblStreamer(args.port, profile.serial_baud) as streamer:
            try:
                streamer.verify_settings(profile.expected_grbl_settings)
                if profile.unlock_command:
                    streamer.command(profile.unlock_command)
                print("checking all programs in GRBL dry-run mode")
                streamer.check_program("\n".join(programs.values()))
                # Leaving $C check mode performs a GRBL soft reset. Restore the
                # explicitly configured unlocked state before homing/running.
                if profile.unlock_command:
                    streamer.command(profile.unlock_command)
                if profile.home_command and not args.skip_homing:
                    streamer.command(profile.home_command, timeout_s=idle_timeout_s)
                    streamer.wait_until_idle(idle_timeout_s)
                streamer.verify_coordinate_offsets(
                    profile.coordinate_system,
                    profile.expected_work_offset_mm,
                )
                # Homing may execute persistent GRBL startup blocks.  Restore
                # the verified raised state before asking the operator to put
                # the first pen anywhere near the holder.
                for command in profile.pen_up_commands:
                    streamer.command(command)
                if profile.motion.pen_lift_s:
                    time.sleep(profile.motion.pen_lift_s)
                for index, (name, program) in enumerate(programs.items(), start=1):
                    if args.bounds_only:
                        prompt = (
                            f"Remove the pen or verify it is fully raised for {name}, "
                            "secure the paper, then press Enter. Ctrl-C holds motion. "
                        )
                    else:
                        pen = job["pen_groups"][index - 1]["pen"]
                        prompt = (
                            f"Load {pen['label']} ({pen['id']}) for {name}, secure "
                            "the pen, then press Enter. Ctrl-C holds motion. "
                        )
                    input(prompt)
                    print(f"streaming {name}")
                    streamer.stream(
                        program,
                        lambda done, total: print(
                            f"\r  {done:>7}/{total:<7} {100 * done / total:5.1f}%",
                            end="",
                            flush=True,
                        ),
                        idle_timeout_s=idle_timeout_s,
                    )
                    print()
            except (KeyboardInterrupt, EOFError):
                streamer.emergency_hold()
                raise PlotJobError(
                    "operator interrupted setup or homing; GRBL feed hold was sent. "
                    "Lift the pen, reset the controller, and re-home before restart."
                ) from None
            except PlotJobError:
                streamer.emergency_hold()
                raise
        return 0
    except PlotJobError as exc:
        print(f"plotter control: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
