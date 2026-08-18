#!/usr/bin/env python3
"""Check a generated plate (SVG + .plot.json) against docs/format/format-v1.json.

Usage:
    python3 tools/validate_format.py output/york.svg [--format a5-portrait]
    python3 tools/validate_format.py examples/*.svg --quiet

Exit status is 0 only when every rule passes, so this drops straight into CI or
a pre-plot check. The format is inferred from the SVG page size unless given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from city_map_plotter.pens import (  # noqa: E402
    ACTUAL_PENS_PROFILE,
    ACTUAL_PEN_INVENTORY,
)
from city_map_plotter.geometry import (  # noqa: E402
    load_plate_format,
    make_a5_balanced_poster_layout,
)
from city_map_plotter.furniture import (  # noqa: E402
    with_split_zones,
)
from city_map_plotter.models import BoundingBox, MapPlotterError  # noqa: E402
from city_map_plotter.render_contract import visual_renderer_contract  # noqa: E402
from city_map_plotter.stroke_font import stroke_font_contract  # noqa: E402
from city_map_plotter.styles import enabled_layer_ids, parse_families  # noqa: E402
from city_map_plotter.svg import _typography_evidence  # noqa: E402
from city_map_plotter.themes import load_theme  # noqa: E402

#: Poster layouts that render the generated crew composition.
CREW_POSTER_LAYOUTS = frozenset({"rowing-crew"})

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

SPEC_PATH = ROOT / "docs" / "format" / "format-v1.json"

# Absolute M/L/C/Z only — the shape this generator emits. A relative or arc
# command is itself a conformance failure, so it is reported rather than parsed.
_TOKEN = re.compile(r"([MLCZmlczAaHhVvSsQqTt])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")

# One SVG user unit is one millimetre.  The adaptive cubic measurement below
# keeps each curve within this absolute error budget.  It is deliberately much
# tighter than the three-decimal SVG coordinate precision and the physical
# 3 x nib threshold used by this validator.
_CUBIC_LENGTH_TOLERANCE_MM = 1e-7
_MAX_CUBIC_SUBDIVISION_DEPTH = 32

Point = tuple[float, float]


@dataclass(frozen=True)
class _ParsedSubpath:
    points: tuple[Point, ...]
    length_mm: float


@dataclass(frozen=True)
class _InventoryPen:
    pen_id: str
    ink: str
    nominal_nib_mm: float


@dataclass(frozen=True)
class _InventoryContext:
    profile_id: str
    pens_by_id: dict[str, _InventoryPen]


@dataclass(frozen=True)
class _PenEvidence:
    subject: str
    profile_id: str | None
    pen_id: str | None
    ink: str | None
    nominal_nib_mm: float | None


class Report:
    def __init__(self, target: str) -> None:
        self.target = target
        self.failures: list[str] = []
        self.warnings: list[str] = []
        #: Measurements reported for information only. Advisories are never
        #: failures and are never escalated by --warnings-as-errors, because
        #: they describe a deliberate studio trade-off rather than a defect.
        self.advisories: list[str] = []
        self.checks = 0

    def check(self, ok: bool, message: str) -> bool:
        self.checks += 1
        if not ok:
            self.failures.append(message)
        return ok

    def warn(self, ok: bool, message: str) -> None:
        self.checks += 1
        if not ok:
            self.warnings.append(message)

    def advise(self, ok: bool, message: str) -> None:
        """Record a measurement without ever gating on it."""

        self.checks += 1
        if not ok:
            self.advisories.append(message)

    @property
    def passed(self) -> bool:
        return not self.failures


def _midpoint(left: Point, right: Point) -> Point:
    return ((left[0] + right[0]) / 2, (left[1] + right[1]) / 2)


def _flatten_cubic(
    start: Point,
    control_1: Point,
    control_2: Point,
    end: Point,
    *,
    tolerance_mm: float,
    depth: int = 0,
) -> tuple[list[Point], float]:
    """Return sampled endpoints and a bounded cubic Bezier arc-length estimate.

    A cubic's chord is a lower bound on its length and its control polygon is
    an upper bound.  De Casteljau subdivision tightens those bounds.  Splitting
    the caller's tolerance between both children keeps the sum within the same
    deterministic absolute error budget.
    """

    chord = math.dist(start, end)
    control_polygon = (
        math.dist(start, control_1)
        + math.dist(control_1, control_2)
        + math.dist(control_2, end)
    )
    gap = max(0.0, control_polygon - chord)
    if gap <= 2 * tolerance_mm or depth >= _MAX_CUBIC_SUBDIVISION_DEPTH:
        return [end], (chord + control_polygon) / 2

    start_control = _midpoint(start, control_1)
    controls_midpoint = _midpoint(control_1, control_2)
    control_end = _midpoint(control_2, end)
    left_control = _midpoint(start_control, controls_midpoint)
    right_control = _midpoint(controls_midpoint, control_end)
    split = _midpoint(left_control, right_control)
    child_tolerance = tolerance_mm / 2
    left_points, left_length = _flatten_cubic(
        start,
        start_control,
        left_control,
        split,
        tolerance_mm=child_tolerance,
        depth=depth + 1,
    )
    right_points, right_length = _flatten_cubic(
        split,
        right_control,
        control_end,
        end,
        tolerance_mm=child_tolerance,
        depth=depth + 1,
    )
    return left_points + right_points, left_length + right_length


def _parse_path(d: str) -> tuple[list[_ParsedSubpath], set[str]]:
    """Return measured subpaths, plus any non-absolute commands seen."""
    commands: list[str] = []
    numbers: list[float] = []
    order: list[tuple[str, int]] = []
    for match in _TOKEN.finditer(d):
        if match.group(1):
            commands.append(match.group(1))
            order.append(("cmd", len(commands) - 1))
        else:
            numbers.append(float(match.group(2)))
            order.append(("num", len(numbers) - 1))

    illegal = {c for c in commands if c not in {"M", "L", "C", "Z"}}
    subpaths: list[_ParsedSubpath] = []
    current: list[Point] = []
    current_length = 0.0
    index = 0
    active = ""
    while index < len(order):
        kind, position = order[index]
        if kind == "cmd":
            active = commands[position]
            if active == "M":
                if len(current) >= 2:
                    subpaths.append(_ParsedSubpath(tuple(current), current_length))
                current = []
                current_length = 0.0
            elif active == "Z":
                if current and current[-1] != current[0]:
                    current_length += math.dist(current[-1], current[0])
                    current.append(current[0])
            index += 1
            continue
        need = {"M": 2, "L": 2, "C": 6}.get(active, 0)
        if need == 0 or position + need > len(numbers):
            index += 1
            continue
        chunk = numbers[position : position + need]
        target = (chunk[-2], chunk[-1])
        if active == "C" and current:
            curve_points, curve_length = _flatten_cubic(
                current[-1],
                (chunk[0], chunk[1]),
                (chunk[2], chunk[3]),
                target,
                tolerance_mm=_CUBIC_LENGTH_TOLERANCE_MM,
            )
            current.extend(curve_points)
            current_length += curve_length
        else:
            # Additional coordinate pairs after M are implicit absolute L
            # commands under the SVG path-data grammar.
            if current:
                current_length += math.dist(current[-1], target)
            current.append(target)
        index += need
    if len(current) >= 2:
        subpaths.append(_ParsedSubpath(tuple(current), current_length))
    return subpaths, illegal


def _segment_length_in_box(start: Point, end: Point, box: tuple[float, ...]) -> float:
    """Return the exact straight-segment length inside a closed rectangle.

    Liang-Barsky clipping gives the entering and leaving parameters without
    constructing replacement geometry.  Treating the rectangle as closed is
    intentional: a map-frame stroke on the field edge consumes ink in the
    field and therefore belongs in the budget.
    """

    x0, y0, x1, y1 = box
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    segment_length = math.hypot(dx, dy)
    if segment_length == 0:
        return 0.0

    enter = 0.0
    leave = 1.0
    for direction, distance_to_edge in (
        (-dx, start[0] - x0),
        (dx, x1 - start[0]),
        (-dy, start[1] - y0),
        (dy, y1 - start[1]),
    ):
        if direction == 0:
            if distance_to_edge < 0:
                return 0.0
            continue
        parameter = distance_to_edge / direction
        if direction < 0:
            enter = max(enter, parameter)
        else:
            leave = min(leave, parameter)
        if enter > leave:
            return 0.0
    return segment_length * max(0.0, leave - enter)


def _polyline_length_in_box(points: tuple[Point, ...], box: tuple[float, ...]) -> float:
    """Measure the portion of a flattened path that physically lies in *box*."""

    return sum(
        _segment_length_in_box(start, end, box)
        for start, end in zip(points, points[1:])
    )


def _mm(value: str | None) -> float | None:
    if not value:
        return None
    match = re.match(r"^\s*(-?\d*\.?\d+)\s*(mm)?\s*$", value)
    return float(match.group(1)) if match else None


def _infer_format(spec: dict, width: float, height: float) -> str | None:
    for key, fmt in spec["formats"].items():
        page = fmt["page_mm"]
        if abs(page["width"] - width) < 0.51 and abs(page["height"] - height) < 0.51:
            return key
    return None


def _layer_groups(root: ET.Element) -> list[ET.Element]:
    return [
        g
        for g in root.findall(f"{{{SVG_NS}}}g")
        if g.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer"
    ]


def _nib_from_label(label: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*$", label.strip())
    return float(match.group(1).replace(",", ".")) if match else None


def _positive_finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_text(value: object) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def _inventory_context_from_pens(
    profile_id: str, pens: list[_InventoryPen]
) -> _InventoryContext:
    return _InventoryContext(
        profile_id=profile_id,
        pens_by_id={pen.pen_id: pen for pen in pens},
    )


_ACTUAL_INVENTORY_CONTEXT = _inventory_context_from_pens(
    ACTUAL_PENS_PROFILE,
    [
        _InventoryPen(
            pen_id=pen.identity,
            ink=pen.ink,
            nominal_nib_mm=pen.nominal_nib_mm,
        )
        for pen in ACTUAL_PEN_INVENTORY.pens
    ],
)


def _inventory_record_context(
    value: object,
    report: Report,
) -> _InventoryContext | None:
    """Parse the inventory embedded in a current plot manifest.

    Legacy manifests did not carry this record. Absence is therefore not a
    failure by itself; malformed explicit inventory evidence is.
    """

    if not isinstance(value, dict):
        report.check(False, "manifest rendering.pen_inventory must be an object")
        return None
    profile_id = _optional_text(value.get("id"))
    records = value.get("pens")
    if not report.check(
        profile_id is not None,
        "manifest pen inventory has no stable id",
    ):
        return None
    if not report.check(
        isinstance(records, list) and bool(records),
        f"manifest pen inventory {profile_id!r} has no non-empty pens list",
    ):
        return None
    assert isinstance(records, list)

    pens: list[_InventoryPen] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            report.check(
                False,
                f"manifest pen inventory {profile_id!r} entry {index} is not an object",
            )
            continue
        pen_id = _optional_text(record.get("id"))
        ink = _optional_text(record.get("ink"))
        nominal_nib = _positive_finite_number(record.get("nominal_nib_mm"))
        valid = report.check(
            pen_id is not None and ink is not None and nominal_nib is not None,
            f"manifest pen inventory {profile_id!r} entry {index} needs a stable "
            "id, ink, and positive nominal_nib_mm",
        )
        if not valid:
            continue
        assert pen_id is not None and ink is not None and nominal_nib is not None
        if not report.check(
            pen_id not in seen_ids,
            f"manifest pen inventory {profile_id!r} repeats pen id {pen_id!r}",
        ):
            continue
        seen_ids.add(pen_id)
        pens.append(_InventoryPen(pen_id, ink, nominal_nib))
    if not pens:
        return None
    assert profile_id is not None
    return _inventory_context_from_pens(profile_id, pens)


def _manifest_inventory_context(
    manifest: dict,
    report: Report,
) -> _InventoryContext | None:
    rendering = manifest.get("rendering")
    if not isinstance(rendering, dict):
        return None
    profile_id = _optional_text(rendering.get("pen_profile"))
    inventory_record = rendering.get("pen_inventory")
    embedded = (
        _inventory_record_context(inventory_record, report)
        if inventory_record is not None
        else None
    )

    if profile_id == ACTUAL_PENS_PROFILE:
        if embedded is not None:
            report.check(
                embedded.profile_id == ACTUAL_PENS_PROFILE,
                "manifest pen profile 'actual-pens' does not match embedded "
                f"inventory {embedded.profile_id!r}",
            )
            expected = {
                (pen.pen_id, pen.ink.casefold(), round(pen.nominal_nib_mm, 6))
                for pen in _ACTUAL_INVENTORY_CONTEXT.pens_by_id.values()
            }
            supplied = {
                (pen.pen_id, pen.ink.casefold(), round(pen.nominal_nib_mm, 6))
                for pen in embedded.pens_by_id.values()
            }
            report.check(
                supplied == expected,
                "embedded 'actual-pens' inventory does not match the real studio "
                "pen IDs and ink/nib combinations",
            )
        return _ACTUAL_INVENTORY_CONTEXT

    if profile_id == "style":
        return None
    if embedded is not None:
        if profile_id is not None:
            report.check(
                embedded.profile_id == profile_id,
                f"manifest pen profile {profile_id!r} does not match embedded "
                f"inventory {embedded.profile_id!r}",
            )
        return embedded
    if profile_id is not None:
        report.check(
            False,
            f"manifest pen profile {profile_id!r} has no embedded inventory evidence",
        )
    return None


def _validate_pen_evidence(
    evidence: _PenEvidence,
    manifest_inventory: _InventoryContext | None,
    report: Report,
) -> None:
    """Validate explicit physical identity without guessing for legacy plates."""

    profile_id = evidence.profile_id
    if (
        profile_id is not None
        and manifest_inventory is not None
        and profile_id != manifest_inventory.profile_id
    ):
        report.check(
            False,
            f"{evidence.subject} uses pen profile {profile_id!r}, not manifest "
            f"profile {manifest_inventory.profile_id!r}",
        )
        return

    effective_profile = profile_id or (
        manifest_inventory.profile_id if manifest_inventory is not None else None
    )
    if effective_profile in {None, "style"}:
        # No inventory identity exists on genuine legacy/style-driven review
        # output, so the older numeric ladder checks remain the only safe claim.
        return
    if effective_profile == ACTUAL_PENS_PROFILE:
        inventory = _ACTUAL_INVENTORY_CONTEXT
    elif (
        manifest_inventory is not None
        and manifest_inventory.profile_id == effective_profile
    ):
        inventory = manifest_inventory
    else:
        report.check(
            False,
            f"{evidence.subject} names pen profile {effective_profile!r} without "
            "matching inventory evidence",
        )
        return

    pen = (
        inventory.pens_by_id.get(evidence.pen_id)
        if evidence.pen_id is not None
        else None
    )
    if evidence.pen_id is not None:
        if not report.check(
            pen is not None,
            f"{evidence.subject} uses pen id {evidence.pen_id!r}, which is not in "
            f"inventory {inventory.profile_id!r}",
        ):
            return
        assert pen is not None
        if evidence.ink is not None:
            report.check(
                evidence.ink.casefold() == pen.ink.casefold(),
                f"{evidence.subject} says ink {evidence.ink!r}, but pen id "
                f"{pen.pen_id!r} is {pen.ink!r}",
            )
        if evidence.nominal_nib_mm is not None:
            report.check(
                abs(evidence.nominal_nib_mm - pen.nominal_nib_mm) < 1e-6,
                f"{evidence.subject} says nominal nib "
                f"{evidence.nominal_nib_mm:g} mm, but pen id {pen.pen_id!r} is "
                f"{pen.nominal_nib_mm:g} mm",
            )
        return

    report.check(
        False,
        f"{evidence.subject} uses inventory profile {inventory.profile_id!r} "
        "without a stable pen id",
    )
    if evidence.ink is None or evidence.nominal_nib_mm is None:
        return
    compatible = any(
        evidence.ink.casefold() == candidate.ink.casefold()
        and abs(evidence.nominal_nib_mm - candidate.nominal_nib_mm) < 1e-6
        for candidate in inventory.pens_by_id.values()
    )
    report.check(
        compatible,
        f"{evidence.subject} requests unavailable {evidence.ink} "
        f"{evidence.nominal_nib_mm:g} in inventory {inventory.profile_id!r}",
    )


_THEME_LAYER_FIELDS = (
    ("ink", "ink"),
    ("preview_color", "preview_color"),
    ("pen_id", "pen_id"),
    ("nominal_nib_mm", "nominal_nib_mm"),
    ("effective_width_mm", "nib_mm"),
    ("target_width_mm", "requested_width_mm"),
    ("stroke_count", "strokes"),
    ("passes", "passes"),
    ("offset_pitch_mm", "offset_pitch_mm"),
    ("plotted_width_mm", "plotted_width_mm"),
    ("width_error_mm", "width_fit_error_mm"),
    ("fit_mode", "width_fit_mode"),
)

_THEME_GROUP_FIELDS = (
    ("ink", "data-plot-ink"),
    ("preview_color", "stroke"),
    ("pen_id", "data-plot-pen-id"),
    ("nominal_nib_mm", "data-plot-nominal-nib-mm"),
    ("effective_width_mm", "data-plot-nib-mm"),
    ("target_width_mm", "data-plot-requested-width-mm"),
    ("stroke_count", "data-plot-strokes"),
    ("passes", "data-plot-passes"),
    ("offset_pitch_mm", "data-plot-offset-pitch-mm"),
    ("plotted_width_mm", "data-plot-width-mm"),
    ("width_error_mm", "data-plot-width-fit-error-mm"),
    ("fit_mode", "data-plot-width-fit-mode"),
)

_THEME_TYPOGRAPHY_GROUPS = {
    "title": "layer-poster_title",
    "subtitle": "layer-poster_subtitle",
    "detail": "layer-poster_details",
    "legend": "layer-map_furniture",
    "attribution": "layer-attribution",
}

_THEME_TYPOGRAPHY_PHYSICAL_LAYERS = {
    "title": "poster_title",
    "subtitle": "poster_subtitle",
    "detail": "poster_details",
    "legend": "map_furniture",
    "attribution": "attribution",
}


def _theme_values_match(actual: object, expected: object) -> bool:
    """Compare manifest/SVG evidence with the contract's serialized precision."""

    if isinstance(expected, bool) or expected is None:
        return actual == expected
    if isinstance(expected, (int, float)):
        actual_number = _finite_number(actual)
        expected_number = _finite_number(expected)
        return (
            actual_number is not None
            and expected_number is not None
            and abs(actual_number - expected_number) < 1e-6
        )
    return actual == expected


def _check_theme_fields(
    *,
    subject: str,
    expected: dict,
    actual: dict,
    bindings: tuple[tuple[str, str], ...],
    report: Report,
) -> None:
    for expected_key, actual_key in bindings:
        expected_value = expected.get(expected_key)
        actual_value = actual.get(actual_key)
        report.check(
            _theme_values_match(actual_value, expected_value),
            f"{subject} field {actual_key!r} is {actual_value!r}, but the design "
            f"contract requires {expected_value!r}",
        )


def _theme_layer_group(
    *,
    expected: dict,
    actual: dict,
    groups_by_id: dict[str, ET.Element],
    inventory_id: str,
    report: Report,
) -> None:
    layer_id = str(expected.get("layer_id"))
    emitted = actual.get("emitted") is True
    group_id = _optional_text(actual.get("svg_group_id"))
    if not emitted:
        report.check(
            group_id is None,
            f"non-emitted themed layer {layer_id!r} names SVG group {group_id!r}",
        )
        return
    if not report.check(
        group_id is not None,
        f"emitted themed layer {layer_id!r} has no svg_group_id",
    ):
        return
    assert group_id is not None
    group = groups_by_id.get(group_id)
    if not report.check(
        group is not None,
        f"emitted themed layer {layer_id!r} references missing SVG group {group_id!r}",
    ):
        return
    assert group is not None
    _check_theme_fields(
        subject=f"themed SVG layer {layer_id!r}",
        expected=expected,
        actual=group.attrib,
        bindings=_THEME_GROUP_FIELDS,
        report=report,
    )
    report.check(
        group.get("data-plot-pen-profile") == inventory_id,
        f"themed SVG layer {layer_id!r} uses pen profile "
        f"{group.get('data-plot-pen-profile')!r}, not contract inventory "
        f"{inventory_id!r}",
    )


def _theme_cap_values(value: object) -> list[float]:
    values = value if isinstance(value, list) else [value]
    result: list[float] = []
    for item in values:
        number = _positive_finite_number(item)
        if number is not None:
            result.append(number)
    return result


def _validate_theme_typography(
    *,
    root: ET.Element,
    contract: dict,
    physical_emission: dict[str, str],
    groups_by_id: dict[str, ET.Element],
    manifest: dict,
    report: Report,
) -> None:
    typography = contract.get("typography")
    evidence = manifest.get("typography_evidence")
    if not report.check(
        isinstance(typography, dict),
        "themed design contract has no typography object",
    ):
        return
    if not report.check(
        isinstance(evidence, dict),
        "themed manifest has no typography_evidence object",
    ):
        return
    assert isinstance(typography, dict) and isinstance(evidence, dict)
    report.check(
        evidence.get("policy_id") == typography.get("policy_id"),
        "typography evidence policy_id does not match the design contract",
    )
    report.check(
        evidence.get("font") == contract.get("font"),
        "typography evidence font identity does not match the design contract",
    )
    roles = typography.get("roles")
    evidence_roles = evidence.get("roles")
    if not report.check(
        isinstance(roles, dict) and isinstance(evidence_roles, dict),
        "themed typography roles/evidence must be objects",
    ):
        return
    assert isinstance(roles, dict) and isinstance(evidence_roles, dict)
    report.check(
        set(evidence_roles) == set(roles),
        "typography evidence roles do not exactly match the design contract",
    )
    font = contract.get("font")
    font_id = font.get("font_id") if isinstance(font, dict) else None
    font_sha = font.get("sha256") if isinstance(font, dict) else None

    for role, expected in roles.items():
        if not isinstance(expected, dict):
            report.check(False, f"themed typography role {role!r} is not an object")
            continue
        actual = evidence_roles.get(role)
        if not report.check(
            isinstance(actual, dict),
            f"themed typography role {role!r} has no evidence object",
        ):
            continue
        assert isinstance(actual, dict)
        for key, expected_value in expected.items():
            report.check(
                _theme_values_match(actual.get(key), expected_value),
                f"typography evidence role {role!r} field {key!r} is "
                f"{actual.get(key)!r}, but the contract requires {expected_value!r}",
            )

        group_id = _THEME_TYPOGRAPHY_GROUPS.get(str(role))
        physical_layer = _THEME_TYPOGRAPHY_PHYSICAL_LAYERS.get(str(role))
        expected_emitted = physical_emission.get(str(physical_layer)) != "forbidden"
        emitted = actual.get("emitted") is True
        report.check(
            emitted == expected_emitted,
            f"typography role {role!r} emitted={emitted}, expected "
            f"emitted={expected_emitted}",
        )
        group = groups_by_id.get(str(group_id)) if group_id is not None else None
        if not emitted:
            report.check(
                group is None,
                f"non-emitted typography role {role!r} still has SVG group {group_id!r}",
            )
            continue
        if not report.check(
            group is not None,
            f"emitted typography role {role!r} has no SVG group {group_id!r}",
        ):
            continue
        assert group is not None
        expected_group_values = {
            "data-theme-role": role,
            "data-theme-zone": expected.get("zone"),
            "data-theme-placement": expected.get("placement"),
            "data-stroke-font-id": font_id,
            "data-stroke-font-sha256": font_sha,
        }
        for attribute, expected_value in expected_group_values.items():
            report.check(
                group.get(attribute) == expected_value,
                f"typography SVG group {group_id!r} attribute {attribute!r} is "
                f"{group.get(attribute)!r}, expected {expected_value!r}",
            )
        report.check(
            actual.get("within_zone") is True,
            f"typography role {role!r} is not evidenced inside its named zone",
        )
        report.check(
            isinstance(actual.get("geometry_bounds_mm"), dict),
            f"typography role {role!r} has no geometry bounds evidence",
        )
        maximum_lines = expected.get("max_lines")
        line_count = actual.get("line_count")
        report.check(
            isinstance(line_count, int)
            and not isinstance(line_count, bool)
            and isinstance(maximum_lines, int)
            and 1 <= line_count <= maximum_lines,
            f"typography role {role!r} line_count {line_count!r} exceeds its "
            f"contract maximum {maximum_lines!r}",
        )
        minimum_cap = _positive_finite_number(expected.get("minimum_cap_height_mm"))
        cap_values = _theme_cap_values(actual.get("actual_cap_height_mm"))
        report.check(
            minimum_cap is not None
            and bool(cap_values)
            and min(cap_values) + 1e-9 >= minimum_cap,
            f"typography role {role!r} cap-height evidence "
            f"{actual.get('actual_cap_height_mm')!r} is below its contract floor "
            f"{expected.get('minimum_cap_height_mm')!r}",
        )

    expected_copy = {
        "title": [manifest.get("title")],
        "subtitle": [manifest.get("subtitle")],
        "detail": list(manifest.get("details") or [])[:3],
    }
    for role, copy in expected_copy.items():
        role_evidence = evidence_roles.get(role)
        if isinstance(role_evidence, dict) and role_evidence.get("emitted") is True:
            report.check(
                all(isinstance(value, str) and value for value in copy)
                and role_evidence.get("source_copy") == copy,
                f"typography role {role!r} source copy is not bound to the "
                "manifest copy block",
            )

    extent = manifest.get("extent_wgs84")
    if not report.check(
        isinstance(extent, dict),
        "themed manifest has no extent for deterministic typography replay",
    ):
        return
    assert isinstance(extent, dict)
    try:
        bbox = BoundingBox(
            west=float(extent["west"]),
            south=float(extent["south"]),
            east=float(extent["east"]),
            north=float(extent["north"]),
        )
        layout = with_split_zones(make_a5_balanced_poster_layout(bbox))
        regenerated = _typography_evidence(root, contract, layout)
    except (KeyError, TypeError, ValueError, MapPlotterError) as exc:
        report.check(
            False,
            "themed typography cannot be regenerated from its source copy and "
            f"placement contract: {exc}",
        )
        return
    report.check(
        regenerated == evidence,
        "typography evidence does not equal independently regenerated source-copy "
        "geometry and placement evidence",
    )


def _stable_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_installed_theme_contract(
    contract: dict,
    manifest: dict,
    report: Report,
) -> None:
    """Rebuild the installed, selected design invariant instead of trusting it."""

    theme_id = _optional_text(contract.get("theme_id"))
    if theme_id is None:
        report.check(False, "themed design contract has no theme_id")
        return
    try:
        theme = load_theme(theme_id)
        plate = load_plate_format(theme.format_id)
        installed_visual_renderer = visual_renderer_contract()
    except (MapPlotterError, OSError, ValueError) as exc:
        report.check(False, f"installed theme contract cannot be resolved: {exc}")
        return

    format_contract = contract.get("format")
    font_contract = contract.get("font")
    inventory_contract = contract.get("inventory")
    map_plan = contract.get("resolved_map_layers")
    physical_plan = contract.get("resolved_physical_layers")
    typography = contract.get("typography")
    rendering = manifest.get("rendering")
    if not report.check(
        all(
            isinstance(value, dict)
            for value in (
                format_contract,
                font_contract,
                inventory_contract,
                typography,
                rendering,
            )
        )
        and isinstance(map_plan, list)
        and isinstance(physical_plan, list),
        "themed design contract is missing installed-contract evidence",
    ):
        return
    assert isinstance(format_contract, dict)
    assert isinstance(font_contract, dict)
    assert isinstance(inventory_contract, dict)
    assert isinstance(map_plan, list)
    assert isinstance(physical_plan, list)
    assert isinstance(typography, dict)
    assert isinstance(rendering, dict)

    report.check(
        contract.get("theme_sha256") == theme.sha256,
        "design contract theme hash does not match the installed selected theme",
    )
    expected_format = {
        "id": theme.format_id,
        "contract_id": theme.format["contract_id"],
        "selected_plate_sha256": _stable_json_sha256(plate),
        "zones": list(theme.format["zones"]),
    }
    for key, value in expected_format.items():
        report.check(
            format_contract.get(key) == value,
            f"design contract format field {key!r} does not match the installed "
            "selected plate",
        )
    report.check(
        font_contract == stroke_font_contract(),
        "design contract font does not match the installed stroke-font geometry",
    )
    report.check(
        contract.get("visual_renderer_contract") == installed_visual_renderer,
        "design contract visual renderer does not match the installed algorithms "
        "and numeric runtimes",
    )
    expected_policies = {
        "copy_policy_id": theme.copy["policy_id"],
        "placement_policy_id": theme.placement_policy_id,
        "source_policy_id": theme.source_policy_id,
        "validation_policy_id": theme.validation_policy_id,
    }
    for key, value in expected_policies.items():
        report.check(
            contract.get(key) == value,
            f"design contract policy {key!r} does not match the installed theme",
        )
    report.check(
        contract.get("batch") == dict(theme.batch),
        "design contract batch rules do not match the installed theme",
    )
    typography_roles = typography.get("roles")
    report.check(
        typography.get("policy_id") == theme.typography["policy_id"]
        and isinstance(typography_roles, dict)
        and set(typography_roles) == set(theme.typography["roles"]),
        "design contract typography roles do not match the installed theme",
    )
    if isinstance(typography_roles, dict):
        for role, installed_role in theme.typography["roles"].items():
            actual_role = typography_roles.get(role)
            report.check(
                isinstance(actual_role, dict)
                and all(
                    actual_role.get(key) == value
                    for key, value in installed_role.items()
                ),
                f"design contract typography role {role!r} drifts from the "
                "installed theme",
            )

    inventory_document = rendering.get("pen_inventory")
    report.check(
        isinstance(inventory_document, dict),
        "themed manifest does not embed its complete pen inventory",
    )
    if not isinstance(inventory_document, dict):
        return
    report.check(
        inventory_contract.get("id") == inventory_document.get("id"),
        "design contract inventory id does not match the embedded inventory",
    )
    report.check(
        inventory_contract.get("sha256") == _stable_json_sha256(inventory_document),
        "design contract inventory hash does not match the embedded inventory",
    )
    report.check(
        inventory_contract.get("stock_tone") == rendering.get("stock_tone"),
        "design contract stock tone does not match rendering evidence",
    )

    canonical_args = list(theme.canonical_export_args)
    try:
        layers_value = canonical_args[canonical_args.index("--layers") + 1]
        expected_map_ids = enabled_layer_ids(parse_families(layers_value))
    except (ValueError, IndexError, MapPlotterError) as exc:
        report.check(False, f"installed theme layer selection is invalid: {exc}")
        return
    map_records = {
        str(record.get("layer_id")): record
        for record in map_plan
        if isinstance(record, dict) and record.get("layer_id") is not None
    }
    theme_map_records = {str(record["id"]): record for record in theme.map_layers}
    drawn_map_ids = {
        layer_id
        for layer_id, record in theme_map_records.items()
        if record.get("draws", True)
    }
    report.check(
        set(theme_map_records) == expected_map_ids,
        "installed theme does not define exactly the layers its canonical "
        "families select",
    )
    report.check(
        len(map_records) == len(map_plan) and set(map_records) == drawn_map_ids,
        "resolved map-layer plan does not exactly match the layers the installed "
        "theme draws",
    )
    pen_records = {
        str(record.get("id")): record
        for record in inventory_document.get("pens", [])
        if isinstance(record, dict) and record.get("id") is not None
    }

    def check_resolved_pen(record: dict, *, subject: str) -> None:
        pen = pen_records.get(str(record.get("pen_id")))
        report.check(pen is not None, f"{subject} names a pen absent from inventory")
        if pen is None:
            return
        nominal = _positive_finite_number(pen.get("nominal_nib_mm"))
        effective = _positive_finite_number(
            pen.get("effective_width_mm", pen.get("nominal_nib_mm"))
        )
        target = _positive_finite_number(record.get("target_width_mm"))
        report.check(
            pen.get("ink") == record.get("ink")
            and _theme_values_match(record.get("nominal_nib_mm"), nominal)
            and _theme_values_match(record.get("effective_width_mm"), effective),
            f"{subject} does not resolve to its embedded physical pen",
        )
        # Weight is n parallel offsets of one exact nominal nib at 0.85 pitch,
        # exactly as the road compiler builds a wide road. It is never a repeat
        # pass over the same line, and it never selects a different pen.
        strokes = record.get("stroke_count")
        weighted = isinstance(strokes, int) and not isinstance(strokes, bool)
        pitch = 0.0
        plotted = effective
        if weighted and effective is not None and strokes > 1:
            pitch = 0.85 * effective
            plotted = effective + (strokes - 1) * pitch
        report.check(
            target is not None
            and effective is not None
            and weighted
            and 1 <= strokes <= 6
            and record.get("passes") == 1
            and _theme_values_match(record.get("offset_pitch_mm"), pitch)
            and _theme_values_match(record.get("plotted_width_mm"), plotted)
            and _theme_values_match(record.get("width_error_mm"), plotted - target)
            and record.get("fit_mode") == "single-nib",
            f"{subject} violates the one-pass exact-nominal theme fit",
        )

    for layer_id in sorted(expected_map_ids):
        actual = map_records.get(layer_id)
        themed = theme_map_records.get(layer_id)
        if not isinstance(actual, dict) or not isinstance(themed, dict):
            continue
        expected_width = float(plate["map_linework_nib_mm"][str(themed["nib_role"])])
        report.check(
            actual.get("ink") == themed.get("ink")
            and actual.get("nib_role") == themed.get("nib_role")
            and actual.get("preview_color") == themed.get("preview_color")
            and actual.get("order") == themed.get("order")
            and _theme_values_match(actual.get("target_width_mm"), expected_width)
            and _theme_values_match(actual.get("nominal_nib_mm"), expected_width),
            f"resolved map layer {layer_id!r} drifts from the installed theme role",
        )
        check_resolved_pen(actual, subject=f"resolved map layer {layer_id!r}")

    physical_records = {
        str(record.get("layer_id")): record
        for record in physical_plan
        if isinstance(record, dict) and record.get("layer_id") is not None
    }
    themed_physical = {
        layer_id: {
            **record,
            "emission": "required" if record["draws"] else "forbidden",
        }
        for layer_id, record in theme.furniture.items()
    }
    report.check(
        len(physical_records) == len(physical_plan)
        and set(physical_records) == set(themed_physical),
        "resolved physical-layer plan does not exactly match the installed theme",
    )
    for layer_id, themed in themed_physical.items():
        actual = physical_records.get(layer_id)
        if not isinstance(actual, dict):
            continue
        expected_width = float(plate["nib_roles_mm"][str(themed["nib_role"])])
        report.check(
            actual.get("ink") == themed.get("ink")
            and actual.get("nib_role") == themed.get("nib_role")
            and actual.get("preview_color") == themed.get("preview_color")
            and actual.get("emission") == themed.get("emission")
            and _theme_values_match(actual.get("target_width_mm"), expected_width)
            and _theme_values_match(actual.get("nominal_nib_mm"), expected_width),
            f"resolved physical layer {layer_id!r} drifts from the installed theme",
        )
        check_resolved_pen(actual, subject=f"resolved physical layer {layer_id!r}")

    typography_physical_layers = {
        "title": "poster_title",
        "subtitle": "poster_subtitle",
        "detail": "poster_details",
        "legend": "map_furniture",
        "attribution": "attribution",
    }
    if isinstance(typography_roles, dict):
        for role, physical_layer_id in typography_physical_layers.items():
            actual_role = typography_roles.get(role)
            physical_record = physical_records.get(physical_layer_id)
            if not isinstance(actual_role, dict) or not isinstance(
                physical_record, dict
            ):
                continue
            nib_role = str(physical_record.get("nib_role", ""))
            target_nib = float(plate["nib_roles_mm"][nib_role])
            effective_nib = _positive_finite_number(
                physical_record.get("effective_width_mm")
            )
            if effective_nib is None:
                continue
            minimum_cap = round(
                max(
                    float(plate["rules"]["min_cap_height_mm"][role]),
                    8.0 * effective_nib,
                ),
                6,
            )
            cap_role = str(actual_role.get("cap_role", role))
            cap_scale = float(actual_role.get("cap_scale", 1.0))
            preferred_cap = round(
                max(float(plate["type_scale_mm"][cap_role]) * cap_scale, minimum_cap),
                6,
            )
            report.check(
                actual_role.get("nib_role") == nib_role
                and actual_role.get("physical_layer_id") == physical_layer_id
                and _theme_values_match(actual_role.get("target_nib_mm"), target_nib)
                and _theme_values_match(
                    actual_role.get("minimum_cap_height_mm"), minimum_cap
                )
                and _theme_values_match(
                    actual_role.get("preferred_cap_height_mm"), preferred_cap
                ),
                f"design contract typography role {role!r} does not match its "
                "installed nib-relative physical floor",
            )

    invariant = {
        "theme_id": theme.id,
        "theme_sha256": theme.sha256,
        "format_id": theme.format_id,
        "format_contract_id": format_contract.get("contract_id"),
        "format_selected_plate_sha256": format_contract.get("selected_plate_sha256"),
        "font_sha256": font_contract.get("sha256"),
        "inventory_id": inventory_contract.get("id"),
        "inventory_sha256": inventory_contract.get("sha256"),
        "stock_tone": inventory_contract.get("stock_tone"),
        "resolved_map_layers": map_plan,
        "resolved_physical_layers": physical_plan,
        "typography_policy_id": typography.get("policy_id"),
        "typography_roles": typography_roles,
        "decoration": contract.get("decoration"),
        "copy_policy_id": contract.get("copy_policy_id"),
        "placement_policy_id": contract.get("placement_policy_id"),
        "source_policy_id": contract.get("source_policy_id"),
        "validation_policy_id": contract.get("validation_policy_id"),
        "visual_renderer_contract_sha256": installed_visual_renderer["sha256"],
    }
    report.check(
        contract.get("edition_signature_sha256") == _stable_json_sha256(invariant),
        "design contract edition signature does not authenticate its selected "
        "semantic invariant",
    )


def _validate_theme_artifact(
    root: ET.Element,
    groups: list[ET.Element],
    manifest: dict,
    report: Report,
) -> None:
    """Bind a themed SVG, manifest, physical plan, and typography as one artifact."""

    root_identity = {
        "theme_id": root.get("data-series-theme"),
        "theme_sha256": root.get("data-series-theme-sha256"),
        "edition_signature_sha256": root.get("data-edition-signature-sha256"),
    }
    contract_value = manifest.get("design_contract")
    themed = contract_value is not None or any(
        value is not None for value in root_identity.values()
    )
    if not themed:
        return
    if not report.check(
        isinstance(contract_value, dict),
        "themed SVG has no manifest design_contract object",
    ):
        return
    contract = contract_value
    assert isinstance(contract, dict)
    _validate_installed_theme_contract(contract, manifest, report)
    css_override_attributes = {
        "class",
        "style",
        "display",
        "visibility",
        "opacity",
        "stroke-opacity",
        "fill-opacity",
        "filter",
        "clip-path",
        "mask",
    }
    path_presentation_attributes = {
        "fill",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "vector-effect",
    }
    unsupported_graphics = {
        "circle",
        "ellipse",
        "foreignObject",
        "image",
        "line",
        "polygon",
        "polyline",
        "rect",
        "text",
        "use",
    }
    active_content = {
        "animate",
        "animateMotion",
        "animateTransform",
        "script",
        "set",
    }
    for element in root.iter():
        local_name = element.tag.rsplit("}", maxsplit=1)[-1]
        report.check(
            local_name not in {"style", "link"},
            "themed SVG contains a stylesheet element",
        )
        report.check(
            local_name not in unsupported_graphics | active_content,
            f"themed SVG contains unsupported drawable or active <{local_name}>; "
            "contracted artwork is path-only",
        )
        forbidden = css_override_attributes & set(element.attrib)
        if element.tag == f"{{{SVG_NS}}}path":
            forbidden |= path_presentation_attributes & set(element.attrib)
        report.check(
            not forbidden,
            "themed SVG contains CSS/presentation overrides: "
            f"{', '.join(sorted(forbidden))}",
        )
    group_identities = {id(group) for group in groups}
    for child in root:
        contains_path = child.tag == f"{{{SVG_NS}}}path" or any(
            descendant.tag == f"{{{SVG_NS}}}path" for descendant in child.iter()
        )
        if contains_path:
            report.check(
                id(child) in group_identities,
                "themed SVG contains plottable geometry outside a top-level "
                "Inkscape layer",
            )
    allowed_group_attributes = {
        "id",
        "fill",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        f"{{{INKSCAPE_NS}}}groupmode",
        f"{{{INKSCAPE_NS}}}label",
    }
    for group in groups:
        group_id = group.get("id")
        report.check(
            group.get("fill") == "none",
            f"themed SVG layer {group_id!r} must use fill='none'",
        )
        unexpected_group_attributes = sorted(
            attribute
            for attribute in group.attrib
            if attribute not in allowed_group_attributes
            and not attribute.startswith("data-")
        )
        report.check(
            not unexpected_group_attributes,
            f"themed SVG layer {group_id!r} has unsupported attributes: "
            f"{unexpected_group_attributes!r}",
        )
        for child in group:
            child_name = child.tag.rsplit("}", maxsplit=1)[-1]
            report.check(
                child.tag in {f"{{{SVG_NS}}}path", f"{{{SVG_NS}}}title"},
                f"themed SVG layer {group_id!r} has nested <{child_name}>; "
                "only direct paths and titles are allowed",
            )
            if child.tag == f"{{{SVG_NS}}}title":
                report.check(
                    not child.attrib and not list(child),
                    f"themed SVG layer {group_id!r} has a non-plain title node",
                )
                continue
            if child.tag != f"{{{SVG_NS}}}path":
                continue
            unexpected_path_attributes = sorted(
                attribute
                for attribute in child.attrib
                if attribute != "d" and not attribute.startswith("data-")
            )
            report.check(
                not unexpected_path_attributes,
                f"themed SVG layer {group_id!r} path has unsupported attributes: "
                f"{unexpected_path_attributes!r}",
            )
            for path_child in child:
                report.check(
                    path_child.tag == f"{{{SVG_NS}}}title"
                    and not path_child.attrib
                    and not list(path_child),
                    f"themed SVG layer {group_id!r} path has an unsupported child",
                )
    for key, root_value in root_identity.items():
        expected_value = contract.get(key)
        report.check(
            root_value == expected_value,
            f"SVG root {key} is {root_value!r}, but manifest design_contract "
            f"requires {expected_value!r}",
        )
    theme_id = _optional_text(contract.get("theme_id"))
    report.check(theme_id is not None, "manifest design_contract has no theme_id")
    for key in ("theme_sha256", "edition_signature_sha256"):
        value = contract.get(key)
        report.check(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
            f"manifest design_contract {key} must be a lowercase SHA-256",
        )

    metadata = root.find(f"{{{SVG_NS}}}metadata")
    metadata_record: object = None
    if metadata is not None:
        try:
            metadata_record = json.loads(metadata.text or "")
        except json.JSONDecodeError:
            metadata_record = None
    if report.check(
        isinstance(metadata_record, dict),
        "themed SVG metadata must be a JSON object",
    ):
        assert isinstance(metadata_record, dict)
        for key in root_identity:
            report.check(
                metadata_record.get(key) == contract.get(key),
                f"SVG metadata {key} does not match the manifest design_contract",
            )

    inventory = contract.get("inventory")
    if not report.check(
        isinstance(inventory, dict) and _optional_text(inventory.get("id")) is not None,
        "themed design contract has no stable inventory identity",
    ):
        return
    assert isinstance(inventory, dict)
    inventory_id = str(inventory["id"])
    rendering = manifest.get("rendering")
    report.check(
        isinstance(rendering, dict) and rendering.get("pen_profile") == inventory_id,
        "themed manifest rendering.pen_profile does not match its design contract",
    )

    map_records = contract.get("resolved_map_layers")
    physical_records = contract.get("resolved_physical_layers")
    layers = manifest.get("layers")
    if not report.check(
        isinstance(map_records, list)
        and isinstance(physical_records, list)
        and isinstance(layers, list),
        "themed design contract and manifest need map, physical, and layer arrays",
    ):
        return
    assert isinstance(map_records, list)
    assert isinstance(physical_records, list)
    assert isinstance(layers, list)
    manifest_layers = [layer for layer in layers if isinstance(layer, dict)]
    report.check(
        len(manifest_layers) == len(layers),
        "themed manifest layers must all be objects",
    )
    actual_group_ids = [
        str(group.get("id")) for group in groups if group.get("id") is not None
    ]
    repeated_actual_group_ids = sorted(
        group_id
        for group_id in set(actual_group_ids)
        if actual_group_ids.count(group_id) != 1
    )
    report.check(
        not repeated_actual_group_ids,
        f"themed SVG repeats top-level layer group IDs: {repeated_actual_group_ids!r}",
    )
    groups_by_id = {
        str(group.get("id")): group for group in groups if group.get("id") is not None
    }

    map_ids: list[str] = []
    for expected in map_records:
        if not isinstance(expected, dict):
            report.check(False, "resolved_map_layers contains a non-object record")
            continue
        layer_id = _optional_text(expected.get("layer_id"))
        if layer_id is None:
            report.check(False, "resolved map layer has no stable layer_id")
            continue
        map_ids.append(layer_id)
        matches = [
            layer
            for layer in manifest_layers
            if str(layer.get("logical_layer_id") or layer.get("id")) == layer_id
        ]
        if not report.check(
            len(matches) == 1,
            f"themed map layer {layer_id!r} has {len(matches)} manifest records, "
            "expected exactly one",
        ):
            continue
        actual = matches[0]
        _check_theme_fields(
            subject=f"themed map layer {layer_id!r}",
            expected=expected,
            actual=actual,
            bindings=_THEME_LAYER_FIELDS,
            report=report,
        )
        report.check(
            actual.get("pen_profile") == inventory_id,
            f"themed map layer {layer_id!r} does not use contract inventory "
            f"{inventory_id!r}",
        )
        _theme_layer_group(
            expected=expected,
            actual=actual,
            groups_by_id=groups_by_id,
            inventory_id=inventory_id,
            report=report,
        )
    report.check(
        len(map_ids) == len(set(map_ids)),
        "resolved_map_layers repeats a layer_id",
    )

    physical_ids: list[str] = []
    physical_emission: dict[str, str] = {}
    for expected in physical_records:
        if not isinstance(expected, dict):
            report.check(False, "resolved_physical_layers contains a non-object record")
            continue
        layer_id = _optional_text(expected.get("layer_id"))
        if layer_id is None:
            report.check(False, "resolved physical layer has no stable layer_id")
            continue
        physical_ids.append(layer_id)
        emission = expected.get("emission")
        physical_emission[layer_id] = str(emission)
        matches = [
            layer
            for layer in manifest_layers
            if layer.get("logical_layer_id") is None and layer.get("id") == layer_id
        ]
        group_id = f"layer-{layer_id}"
        if emission == "forbidden":
            report.check(
                not matches,
                f"design contract forbids themed physical layer {layer_id!r}, but "
                "the manifest contains it",
            )
            report.check(
                group_id not in groups_by_id,
                f"design contract forbids themed physical layer {layer_id!r}, but "
                f"SVG group {group_id!r} exists",
            )
            continue
        if not report.check(
            emission == "required" and len(matches) == 1,
            f"required themed physical layer {layer_id!r} has {len(matches)} "
            "manifest records",
        ):
            continue
        actual = matches[0]
        report.check(
            actual.get("emitted") is True,
            f"required themed physical layer {layer_id!r} is not emitted",
        )
        _check_theme_fields(
            subject=f"themed physical layer {layer_id!r}",
            expected=expected,
            actual=actual,
            bindings=_THEME_LAYER_FIELDS,
            report=report,
        )
        report.check(
            actual.get("pen_profile") == inventory_id,
            f"themed physical layer {layer_id!r} does not use contract inventory "
            f"{inventory_id!r}",
        )
        _theme_layer_group(
            expected=expected,
            actual=actual,
            groups_by_id=groups_by_id,
            inventory_id=inventory_id,
            report=report,
        )
    report.check(
        len(physical_ids) == len(set(physical_ids)),
        "resolved_physical_layers repeats a layer_id",
    )
    expected_manifest_ids = set(map_ids) | {
        str(record.get("layer_id"))
        for record in physical_records
        if isinstance(record, dict) and record.get("emission") == "required"
    }
    actual_manifest_ids = [
        str(layer.get("logical_layer_id") or layer.get("id"))
        for layer in manifest_layers
    ]
    report.check(
        len(actual_manifest_ids) == len(set(actual_manifest_ids)),
        "themed physical manifest repeats a layer identity",
    )
    report.check(
        set(actual_manifest_ids) == expected_manifest_ids,
        "themed physical manifest layers do not exactly match the design contract",
    )

    emitted_manifest_layers = [
        layer
        for layer in manifest_layers
        if layer.get("emitted") is True and layer.get("svg_group_id") is not None
    ]
    declared_group_ids = [
        str(layer["svg_group_id"]) for layer in emitted_manifest_layers
    ]
    report.check(
        len(declared_group_ids) == len(set(declared_group_ids)),
        "themed physical manifest repeats an emitted SVG group identity",
    )
    report.check(
        set(actual_group_ids) == set(declared_group_ids),
        "themed SVG top-level layer groups do not exactly match the physical manifest",
    )
    for layer in emitted_manifest_layers:
        group_id = str(layer["svg_group_id"])
        emitted_group = groups_by_id.get(group_id)
        raw_path_count = layer.get("path_count")
        valid_path_count = (
            isinstance(raw_path_count, int)
            and not isinstance(raw_path_count, bool)
            and raw_path_count > 0
        )
        report.check(
            valid_path_count,
            f"themed manifest layer {group_id!r} has invalid path_count "
            f"{raw_path_count!r}",
        )
        if emitted_group is not None and valid_path_count:
            actual_path_count = len(list(emitted_group.iter(f"{{{SVG_NS}}}path")))
            report.check(
                actual_path_count == raw_path_count,
                f"themed SVG layer {group_id!r} has {actual_path_count} paths, "
                f"but its manifest declares {raw_path_count}",
            )
    _validate_theme_typography(
        root=root,
        contract=contract,
        physical_emission=physical_emission,
        groups_by_id=groups_by_id,
        manifest=manifest,
        report=report,
    )


def _minimum_stroke_rules(fmt: dict) -> tuple[dict[float, float], float]:
    """Read the selected format's generated nib-relative stroke floors.

    Inventory nibs use their exact generated floor.  Calibrated effective mark
    widths can sit between inventory sizes, so their floor uses the most
    conservative multiplier represented by the same generated table.
    """

    raw_rules = (fmt.get("rules") or {}).get("min_stroke_mm_by_nib")
    if not isinstance(raw_rules, dict) or not raw_rules:
        raise ValueError("rules.min_stroke_mm_by_nib is missing or empty")

    floors: dict[float, float] = {}
    multipliers: list[float] = []
    for raw_nib, raw_floor in raw_rules.items():
        nib = _positive_finite_number(raw_nib)
        floor = _positive_finite_number(raw_floor)
        if nib is None or floor is None:
            raise ValueError(
                "rules.min_stroke_mm_by_nib must contain positive numeric nibs "
                "and floors"
            )
        floors[nib] = floor
        multipliers.append(floor / nib)
    return floors, max(multipliers)


def _minimum_stroke_mm(
    nib_mm: float, floors: dict[float, float], multiplier: float
) -> float:
    for rule_nib, floor in floors.items():
        if abs(nib_mm - rule_nib) < 1e-9:
            return floor
    return nib_mm * multiplier


def validate(svg_path: Path, spec: dict, forced: str | None) -> Report:
    report = Report(str(svg_path))
    try:
        tree = ET.parse(svg_path)
    except ET.ParseError as exc:
        report.failures.append(f"SVG is not well-formed XML: {exc}")
        return report
    root = tree.getroot()

    width = _mm(root.get("width"))
    height = _mm(root.get("height"))
    if width is None or height is None:
        report.failures.append(
            f"width/height must be plain millimetres, got "
            f"width={root.get('width')!r} height={root.get('height')!r}"
        )
        return report

    key = forced or _infer_format(spec, width, height)
    if key is None:
        report.failures.append(
            f"page {width}x{height} mm matches no format in {spec['id']}; "
            f"known: {', '.join(spec['formats'])}"
        )
        return report
    fmt = spec["formats"][key]
    report.target = f"{svg_path}  [{key}]"
    try:
        stroke_floors, stroke_multiplier = _minimum_stroke_rules(fmt)
    except ValueError as exc:
        report.check(False, f"selected format has invalid minimum-stroke rules: {exc}")
        stroke_floors, stroke_multiplier = {}, 0.0

    # --- page geometry -----------------------------------------------------
    page = fmt["page_mm"]
    report.check(
        abs(width - page["width"]) < 0.01 and abs(height - page["height"]) < 0.01,
        f"page size {width}x{height} != {page['width']}x{page['height']} mm",
    )
    expected_viewbox = [0.0, 0.0, page["width"], page["height"]]
    try:
        viewbox = [
            float(v) for v in re.split(r"[ ,]+", (root.get("viewBox") or "").strip())
        ]
    except ValueError:
        viewbox = []
    report.check(
        len(viewbox) == 4
        and all(abs(a - b) < 0.01 for a, b in zip(viewbox, expected_viewbox)),
        f"viewBox must be '0 0 {page['width']:g} {page['height']:g}' (1 unit = 1 mm), got {root.get('viewBox')!r}",
    )

    # --- Inkscape scaffolding ---------------------------------------------
    namedview = root.find(
        "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}namedview"
    )
    report.warn(
        namedview is not None,
        "no <sodipodi:namedview>: Inkscape will open the file in px with no current layer",
    )
    if namedview is not None:
        report.warn(
            namedview.get(f"{{{INKSCAPE_NS}}}document-units") == "mm",
            "namedview should set inkscape:document-units='mm'",
        )

    # --- layers, pens, ordering -------------------------------------------
    groups = _layer_groups(root)
    report.check(bool(groups), "no inkscape:groupmode='layer' groups found")

    allowed_nibs = set(fmt["nib_ladder_mm"])
    safe = fmt["safe_margin_mm"]
    safe_box = (safe, safe, page["width"] - safe, page["height"] - safe)

    field = fmt["zones_mm"]["map_field"]
    field_box = (
        field["x"],
        field["y"],
        field["x"] + field["width"],
        field["y"] + field["height"],
    )
    ink_mm2 = [0.0]
    group_pen_evidence: list[_PenEvidence] = []

    pen_runs: list[str] = []
    for group in groups:
        label = group.get(f"{{{INKSCAPE_NS}}}label") or group.get("id") or "?"
        paths = group.findall(f".//{{{SVG_NS}}}path")

        report.check(
            bool(paths), f"layer '{label}' is empty — do not emit zero-path layers"
        )

        label_nib = _nib_from_label(label)
        effective_raw = group.get("data-plot-nib-mm")
        effective_nib = (
            _positive_finite_number(effective_raw)
            if effective_raw is not None
            else label_nib
        )
        if effective_raw is not None:
            report.check(
                effective_nib is not None,
                f"layer '{label}' has invalid effective nib width {effective_raw!r}",
            )

        nominal_raw = group.get("data-plot-nominal-nib-mm")
        nominal_nib = (
            _positive_finite_number(nominal_raw)
            if nominal_raw is not None
            else label_nib or effective_nib
        )
        if nominal_raw is not None:
            report.check(
                nominal_nib is not None,
                f"layer '{label}' has invalid nominal nib width {nominal_raw!r}",
            )
        group_profile = _optional_text(group.get("data-plot-pen-profile"))
        group_pen_id = _optional_text(group.get("data-plot-pen-id"))
        group_ink = _optional_text(group.get("data-plot-ink"))
        group_pen_evidence.append(
            _PenEvidence(
                subject=f"SVG layer {label!r}",
                profile_id=group_profile,
                pen_id=group_pen_id,
                ink=group_ink,
                nominal_nib_mm=nominal_nib,
            )
        )

        # Calibrated effective marks can legitimately fall between inventory
        # sizes.  The physical pen's nominal nib, not that measured mark, is the
        # value constrained by the studio ladder.
        if nominal_nib is not None:
            report.check(
                any(abs(nominal_nib - candidate) < 1e-6 for candidate in allowed_nibs),
                f"layer '{label}' uses nominal nib {nominal_nib:g} mm, not in the "
                f"{fmt['sheet']} ladder "
                f"{fmt['nib_ladder_mm']}",
            )
        else:
            report.warn(
                False,
                f"layer '{label}' has no nominal nib attribute and does not end in a nib width",
            )

        plotted_raw = group.get("data-plot-width-mm")
        if plotted_raw is not None:
            plotted_width = _positive_finite_number(plotted_raw)
            report.check(
                plotted_width is not None,
                f"layer '{label}' has invalid plotted width {plotted_raw!r}",
            )
            if plotted_width is not None and effective_nib is not None:
                report.check(
                    plotted_width + 1e-6 >= effective_nib,
                    f"layer '{label}' plotted width {plotted_width:g} mm is narrower "
                    f"than its {effective_nib:g} mm effective mark",
                )

        if paths:
            pen_key = label.split("—")[-1].strip() if "—" in label else label
            if not pen_runs or pen_runs[-1] != pen_key:
                pen_runs.append(pen_key)

        short_count = 0
        outside_count = 0
        illegal_all: set[str] = set()
        path_pen_evidence: set[
            tuple[str | None, str | None, str | None, float | None]
        ] = set()
        layer_nib = effective_nib or nominal_nib or min(allowed_nibs)
        min_stroke = _minimum_stroke_mm(layer_nib, stroke_floors, stroke_multiplier)
        for path in paths:
            path_profile = _optional_text(path.get("data-plot-pen-profile"))
            path_pen_id = _optional_text(path.get("data-plot-pen-id"))
            path_ink = _optional_text(path.get("data-plot-ink"))
            path_nominal_raw = path.get("data-plot-nominal-nib-mm")
            path_nominal = (
                _positive_finite_number(path_nominal_raw)
                if path_nominal_raw is not None
                else None
            )
            if path_nominal_raw is not None:
                report.check(
                    path_nominal is not None,
                    f"path metadata in layer {label!r} has invalid nominal nib "
                    f"{path_nominal_raw!r}",
                )
            if any(
                value is not None
                for value in (
                    path_profile,
                    path_pen_id,
                    path_ink,
                    path_nominal_raw,
                )
            ):
                path_pen_evidence.add(
                    (path_profile, path_pen_id, path_ink, path_nominal)
                )
            subpaths, illegal = _parse_path(path.get("d", ""))
            illegal_all |= illegal
            for subpath in subpaths:
                ink_mm2[0] += (
                    _polyline_length_in_box(subpath.points, field_box) * layer_nib
                )
                if subpath.length_mm < min_stroke:
                    short_count += 1
                for x, y in subpath.points:
                    if not (
                        safe_box[0] - 0.05 <= x <= safe_box[2] + 0.05
                        and safe_box[1] - 0.05 <= y <= safe_box[3] + 0.05
                    ):
                        outside_count += 1
                        break
        report.check(
            short_count == 0,
            f"layer '{label}': {short_count} sub-nib strokes shorter than "
            f"{min_stroke:.2f} mm ({stroke_multiplier:g} x nib) — these plot as dots",
        )
        report.check(
            outside_count == 0,
            f"layer '{label}': {outside_count} strokes leave the {safe:g} mm plotter-safe area",
        )
        report.check(
            not illegal_all,
            f"layer '{label}': path data uses {sorted(illegal_all)}; only absolute M/L/C/Z allowed",
        )
        for path_profile, path_pen_id, path_ink, path_nominal in sorted(
            path_pen_evidence,
            key=lambda item: tuple(
                "" if value is None else str(value) for value in item
            ),
        ):
            group_pen_evidence.append(
                _PenEvidence(
                    subject=f"path metadata in SVG layer {label!r}",
                    profile_id=path_profile,
                    pen_id=path_pen_id,
                    ink=path_ink,
                    nominal_nib_mm=path_nominal,
                )
            )
            if group_profile is not None and path_profile is not None:
                report.check(
                    path_profile == group_profile,
                    f"path metadata in layer {label!r} uses pen profile "
                    f"{path_profile!r}, not its layer profile {group_profile!r}",
                )
            if group_pen_id is not None and path_pen_id is not None:
                report.check(
                    path_pen_id == group_pen_id,
                    f"path metadata in layer {label!r} uses pen id {path_pen_id!r}, "
                    f"not its layer pen id {group_pen_id!r}",
                )
            if group_ink is not None and path_ink is not None:
                report.check(
                    path_ink.casefold() == group_ink.casefold(),
                    f"path metadata in layer {label!r} uses ink {path_ink!r}, "
                    f"not its layer ink {group_ink!r}",
                )
            if nominal_nib is not None and path_nominal is not None:
                report.check(
                    abs(path_nominal - nominal_nib) < 1e-6,
                    f"path metadata in layer {label!r} uses nominal nib "
                    f"{path_nominal:g} mm, not its layer nib {nominal_nib:g} mm",
                )

    # one pen appearing in two non-adjacent runs == an avoidable pen change
    duplicated = [pen for pen in set(pen_runs) if pen_runs.count(pen) > 1]
    report.check(
        not duplicated,
        f"document order requires reloading {sorted(duplicated)} — emit layers in pen order "
        f"({len(pen_runs)} pen loads for {len(set(pen_runs))} pens)",
    )

    budget = fmt.get("ink_budget")
    if budget:
        coverage = ink_mm2[0] / budget["field_area_mm2"]
        # ADVISORY ONLY, deliberately. This studio prefers density to the
        # legibility reference figure: coverage is measured and reported so the
        # trade-off is visible, and it is never a failure, never escalated by
        # --warnings-as-errors, and never a reason to cull detail or change
        # sheet. Choose the sheet for the design, not for this number.
        report.advise(
            coverage <= budget["max_coverage"] + 1e-9,
            f"map field ink coverage {100 * coverage:.1f}% "
            f"({ink_mm2[0]:,.0f} mm² of a {budget['max_ink_mm2']:,.0f} mm² "
            f"reference budget at {100 * budget['max_coverage']:.0f}%) — "
            f"advisory only, not a defect",
        )

    # --- manifest ----------------------------------------------------------
    manifest_path = svg_path.with_suffix(".plot.json")
    if not manifest_path.exists():
        for evidence in group_pen_evidence:
            _validate_pen_evidence(evidence, None, report)
        report.warn(False, f"no manifest at {manifest_path.name}")
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        for evidence in group_pen_evidence:
            _validate_pen_evidence(evidence, None, report)
        report.failures.append(f"manifest unreadable: {exc}")
        return report

    manifest_inventory = _manifest_inventory_context(manifest, report)
    for evidence in group_pen_evidence:
        _validate_pen_evidence(evidence, manifest_inventory, report)
    _validate_theme_artifact(root, groups, manifest, report)

    zones = (manifest.get("page") or {}).get("zones_mm") or {}
    expected_zones = dict(fmt["zones_mm"])
    # A crew plate is a different composition, not a broken one: it gives map
    # height back to a boat plan and a crew list, and the spec derives that
    # stack alongside the default one. Check it against the stack it actually
    # used, and require every band of that stack to be present.
    poster_layout = str((manifest.get("rendering") or {}).get("poster_layout", ""))
    crew_bands = fmt.get("crew_zones_mm") or {}
    if poster_layout in CREW_POSTER_LAYOUTS and crew_bands:
        expected_zones.update(crew_bands)
        expected_zones["map_field"] = crew_bands["crew_map_field"]
    bridge_bands = fmt.get("bridge_zones_mm") or {}
    if manifest.get("domain") == "bridges" and bridge_bands:
        expected_zones.update(bridge_bands)
    technical_bands = fmt.get("technical_zones_mm") or {}
    if manifest.get("domain") == "technical-objects" and technical_bands:
        expected_zones.update(technical_bands)
    circuit_bands = fmt.get("circuit_zones_mm") or {}
    if manifest.get("domain") in {"f1-circuits", "motorsport-circuits"} and circuit_bands:
        expected_zones.update(circuit_bands)
    for name, rect in expected_zones.items():
        found = zones.get(name)
        if found is None:
            report.check(False, f"manifest is missing zone '{name}'")
            continue
        for axis in ("x", "y", "width", "height"):
            report.check(
                abs(float(found.get(axis, -999)) - rect[axis]) < 0.51,
                f"zone '{name}'.{axis} = {found.get(axis)} mm, spec requires {rect[axis]} mm",
            )

    for layer in manifest.get("layers", []):
        effective_raw = layer.get("nib_mm")
        report.check(
            effective_raw is not None,
            f"manifest layer '{layer.get('id')}' has no machine-readable nib_mm",
        )
        effective_nib = _positive_finite_number(effective_raw)
        if effective_raw is not None:
            report.check(
                effective_nib is not None,
                f"manifest layer '{layer.get('id')}' has invalid effective nib "
                f"width {effective_raw!r}",
            )

        nominal_raw = layer.get("nominal_nib_mm", effective_raw)
        nominal_nib = _positive_finite_number(nominal_raw)
        if "nominal_nib_mm" in layer:
            report.check(
                nominal_nib is not None,
                f"manifest layer '{layer.get('id')}' has invalid nominal nib "
                f"width {nominal_raw!r}",
            )
        if nominal_nib is not None:
            report.check(
                any(abs(nominal_nib - c) < 1e-6 for c in allowed_nibs),
                f"manifest layer '{layer.get('id')}' nominal nib {nominal_raw} "
                f"not in {fmt['nib_ladder_mm']}",
            )
        _validate_pen_evidence(
            _PenEvidence(
                subject=f"manifest layer {layer.get('id')!r}",
                profile_id=_optional_text(layer.get("pen_profile")),
                pen_id=_optional_text(layer.get("pen_id")),
                ink=_optional_text(layer.get("ink")),
                nominal_nib_mm=nominal_nib,
            ),
            manifest_inventory,
            report,
        )

        plotted_raw = layer.get("plotted_width_mm")
        if plotted_raw is not None:
            plotted_width = _positive_finite_number(plotted_raw)
            report.check(
                plotted_width is not None,
                f"manifest layer '{layer.get('id')}' has invalid plotted width "
                f"{plotted_raw!r}",
            )
            if plotted_width is not None and effective_nib is not None:
                report.check(
                    plotted_width + 1e-6 >= effective_nib,
                    f"manifest layer '{layer.get('id')}' plotted width "
                    f"{plotted_width:g} mm is narrower than its "
                    f"{effective_nib:g} mm effective mark",
                )

    for index, step in enumerate(manifest.get("pen_sequence", []), start=1):
        if not isinstance(step, dict):
            report.check(False, f"manifest pen_sequence entry {index} is not an object")
            continue
        step_nominal_raw = step.get("nominal_nib_mm", step.get("nib_mm"))
        step_nominal = _positive_finite_number(step_nominal_raw)
        if step_nominal_raw is not None:
            report.check(
                step_nominal is not None,
                f"manifest pen_sequence entry {index} has invalid nominal nib "
                f"{step_nominal_raw!r}",
            )
        _validate_pen_evidence(
            _PenEvidence(
                subject=f"manifest pen_sequence entry {index}",
                profile_id=_optional_text(step.get("pen_profile")),
                pen_id=_optional_text(step.get("pen_id")),
                ink=_optional_text(step.get("ink")),
                nominal_nib_mm=step_nominal,
            ),
            manifest_inventory,
            report,
        )

    report.warn(
        any(
            "pen_up" in str(k)
            for step in manifest.get("pen_sequence", [])
            for k in step
        ),
        "pen_sequence reports no pen-up travel; the plan is not schedulable",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", nargs="+", type=Path)
    parser.add_argument("--format", dest="forced", help="force a format id")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--quiet", action="store_true", help="only show failures")
    parser.add_argument(
        "--warnings-as-errors", action="store_true", help="fail on warnings too"
    )
    args = parser.parse_args(argv)

    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read spec {args.spec}: {exc}", file=sys.stderr)
        return 2

    worst = 0
    for path in args.svg:
        report = validate(path, spec, args.forced)
        failed = report.failures or (args.warnings_as_errors and report.warnings)
        if failed:
            worst = 1
        if args.quiet and not failed:
            continue
        mark = "FAIL" if failed else "PASS"
        print(f"[{mark}] {report.target}  ({report.checks} checks)")
        for message in report.failures:
            print(f"    x {message}")
        for message in report.warnings:
            print(f"    ! {message}")
        for message in report.advisories:
            print(f"    i {message}")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
