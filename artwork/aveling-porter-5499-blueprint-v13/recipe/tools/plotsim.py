#!/usr/bin/env python3
"""Pen-plotter motion simulator.

Reads a generated plate SVG and produces the plot program a real machine would
execute: ordered pen changes, pen-up travels and pen-down draws, with timings
from a trapezoidal motion planner using junction-deviation cornering -- the same
scheme AxiDraw/Grbl-class controllers use.

    python3 tools/plotsim.py examples/york-a5-clean-poster.svg
    python3 tools/plotsim.py plate.svg --order optimised --json timeline.json
    python3 tools/plotsim.py plate.svg --compare

The timeline JSON drives the animated viewer (tools/plotsim_viewer.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
UNSAFE_TO_PARSE_CODES = frozenset({"unsafe-xml-declaration", "invalid-xml", "not-svg"})
NON_SIMULATABLE_SVG_CODES = UNSAFE_TO_PARSE_CODES | frozenset(
    {
        "invalid-page-size",
        "missing-viewbox",
        "invalid-viewbox",
        "non-millimetre-viewbox",
    }
)

_TOKEN = re.compile(r"([MLCZmlczHhVv])|([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")
_PEN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not accepted")


# ---------------------------------------------------------------------------
# Machine model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Machine:
    """Measured or nominal motion model used by simulation and execution.

    The default is an isotropic AxiDraw-class approximation. A GRBL device uses
    ``grbl-cartesian`` so the planner resolves its configured X/Y acceleration
    along each segment and junction. A real machine should be described by a
    versioned JSON profile and calibrated against timed plots. ``timing_scale``
    applies only to motor motion; pen servo and human change delays stay explicit.
    """

    name: str = "AxiDraw-class"
    pen_down_speed_mm_s: float = 80.0
    pen_up_speed_mm_s: float = 230.0
    acceleration_mm_s2: float = 1500.0
    motion_model: str = "isotropic"
    pen_lift_s: float = 0.22
    pen_lower_s: float = 0.22
    pen_change_s: float = 25.0  # human swaps the pen
    cornering_tolerance_mm: float = 0.05
    curve_flatness_mm: float = 0.02  # Bezier flattening tolerance
    command_latency_s: float = 0.0
    timing_scale: float = 1.0
    timing_uncertainty_fraction: float = 0.15
    calibration_state: str = "nominal-unmeasured"
    work_width_mm: float = 430.0
    work_height_mm: float = 310.0
    allow_page_rotation: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("machine name must be a non-empty string")
        if not isinstance(self.calibration_state, str) or not self.calibration_state:
            raise ValueError("machine calibration_state must be a non-empty string")
        if not isinstance(self.allow_page_rotation, bool):
            raise ValueError("machine allow_page_rotation must be true or false")
        if self.motion_model not in {"isotropic", "grbl-cartesian"}:
            raise ValueError("machine motion_model must be isotropic or grbl-cartesian")
        positive = {
            "pen_down_speed_mm_s": self.pen_down_speed_mm_s,
            "pen_up_speed_mm_s": self.pen_up_speed_mm_s,
            "acceleration_mm_s2": self.acceleration_mm_s2,
            "cornering_tolerance_mm": self.cornering_tolerance_mm,
            "curve_flatness_mm": self.curve_flatness_mm,
            "timing_scale": self.timing_scale,
            "work_width_mm": self.work_width_mm,
            "work_height_mm": self.work_height_mm,
        }
        for key, value in positive.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"machine {key} must be finite and greater than zero")
        nonnegative = {
            "pen_lift_s": self.pen_lift_s,
            "pen_lower_s": self.pen_lower_s,
            "pen_change_s": self.pen_change_s,
            "command_latency_s": self.command_latency_s,
            "timing_uncertainty_fraction": self.timing_uncertainty_fraction,
        }
        for key, value in nonnegative.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"machine {key} must be finite and non-negative")
        if self.timing_uncertainty_fraction > 1:
            raise ValueError("machine timing uncertainty must not exceed 1.0")

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "Machine":
        """Load either a bare motion mapping or a device-profile ``motion`` block."""

        motion = raw.get("motion", raw)
        if not isinstance(motion, dict):
            raise ValueError("machine profile motion must be an object")
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        values = {key: value for key, value in motion.items() if key in allowed}
        if "name" not in values and isinstance(raw.get("name"), str):
            values["name"] = raw["name"]
        work = raw.get("work_area_mm")
        if isinstance(work, dict):
            values.setdefault("work_width_mm", work.get("width"))
            values.setdefault("work_height_mm", work.get("height"))
        values = {key: value for key, value in values.items() if value is not None}
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid machine motion profile: {exc}") from exc

    @classmethod
    def from_json(cls, path: Path) -> "Machine":
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not read machine profile {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"machine profile {path} must contain a JSON object")
        return cls.from_mapping(raw)

    def as_dict(self) -> dict[str, Any]:
        return {
            field.name: getattr(self, field.name)
            for field in self.__dataclass_fields__.values()
        }


@dataclass(frozen=True)
class Pen:
    """One pen load, as the plate declares it rather than as its label reads.

    `nib_mm` is the width the pen actually marks -- the calibrated effective
    width where one was measured, the nominal barrel size otherwise.  It is
    what the simulator draws with; `nominal_mm` only names the pen.
    """

    id: str
    ink: str
    nib_mm: float
    nominal_mm: float
    colour: str
    label: str
    calibration: str = "nominal-unmeasured"

    @property
    def measured(self) -> bool:
        return self.calibration == "measured"


@dataclass
class Stroke:
    layer: str
    pen: Pen
    points: list[tuple[float, float]]
    sid: int = -1  # index into the shared geometry table
    rev: bool = False  # True when the optimiser plotted it end-to-start

    @property
    def start(self) -> tuple[float, float]:
        return self.points[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.points[-1]

    @property
    def nib_mm(self) -> float:
        return self.pen.nib_mm

    @property
    def colour(self) -> str:
        return self.pen.colour

    def reversed_copy(self) -> "Stroke":
        return Stroke(
            self.layer,
            self.pen,
            self.points[::-1],
            self.sid,
            not self.rev,
        )


@dataclass
class Move:
    kind: str  # draw | travel | penchange
    pen: Pen
    layer: str
    points: list[tuple[float, float]]
    t_start: float = 0.0
    duration: float = 0.0
    cumulative: list[float] = field(default_factory=list)

    @property
    def colour(self) -> str:
        return self.pen.colour

    @property
    def nib_mm(self) -> float:
        return self.pen.nib_mm


@dataclass(frozen=True)
class SvgIssue:
    severity: str  # error | warning | info
    code: str
    message: str
    element: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.element:
            value["element"] = self.element
        return value


@dataclass(frozen=True)
class SvgPreflight:
    source_sha256: str
    source_bytes: int
    width_mm: float
    height_mm: float
    path_count: int
    layer_count: int
    metadata_complete: bool
    issues: tuple[SvgIssue, ...]

    @property
    def errors(self) -> tuple[SvgIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[SvgIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def hardware_safe(self) -> bool:
        return not self.errors and self.metadata_complete

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "source_bytes": self.source_bytes,
            "page_mm": {"width": self.width_mm, "height": self.height_mm},
            "path_count": self.path_count,
            "layer_count": self.layer_count,
            "metadata_complete": self.metadata_complete,
            "hardware_safe": self.hardware_safe,
            "issues": [issue.as_dict() for issue in self.issues],
        }


_LENGTH_UNIT_TO_MM = {
    "": 1.0,
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
    "px": 25.4 / 96.0,
    "q": 0.25,
}
_LENGTH = re.compile(
    r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)\s*$"
)


def _physical_length_mm(value: str | None, fallback: float) -> tuple[float, str]:
    match = _LENGTH.match(value or "")
    if not match:
        return fallback, ""
    unit = match.group(2).casefold()
    multiplier = _LENGTH_UNIT_TO_MM.get(unit)
    if multiplier is None:
        return fallback, unit
    return float(match.group(1)) * multiplier, unit


def _strict_physical_length_mm(value: str | None) -> tuple[float | None, str]:
    """Parse an explicit physical SVG length without inventing a fallback."""

    match = _LENGTH.match(value or "")
    if not match:
        return None, ""
    unit = match.group(2).casefold()
    multiplier = _LENGTH_UNIT_TO_MM.get(unit)
    if multiplier is None or unit == "":
        return None, unit
    result = float(match.group(1)) * multiplier
    if not math.isfinite(result) or result <= 0:
        return None, unit
    return result, unit


def _supported_path_syntax_error(data: str) -> str | None:
    """Return why supported path data is malformed, or ``None`` when valid."""

    matches = list(_TOKEN.finditer(data))
    residue = _TOKEN.sub("", data)
    if re.sub(r"[\s,]+", "", residue):
        return "Path data contains invalid or unsupported syntax."
    if not matches or not matches[0].group(1) or matches[0].group(1).upper() != "M":
        return "Path data must begin with an M/m command."

    active: str | None = None
    values = 0

    def command_error(command: str | None, count: int) -> str | None:
        if command is None:
            return None
        upper = command.upper()
        if upper == "Z":
            return None if count == 0 else "Z/z cannot have coordinate parameters."
        arity = {"M": 2, "L": 2, "C": 6, "H": 1, "V": 1}[upper]
        if count == 0 or count % arity:
            return f"{command} requires coordinate groups of {arity}."
        return None

    for match in matches:
        if match.group(1):
            error = command_error(active, values)
            if error:
                return error
            active = match.group(1)
            values = 0
        elif active is None:
            return "Path coordinates appear before the first command."
        else:
            if not math.isfinite(float(match.group(2))):
                return "Path coordinates must be finite numbers."
            values += 1
    return command_error(active, values)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def preflight_svg(path: Path, machine: Machine | None = None) -> SvgPreflight:
    """Fail-closed structural audit for simulation and hardware compilation.

    City Map Plotter output is deliberately simple: millimetre page geometry,
    physical Inkscape layers, and already-expanded path linework.  The viewer
    may display a less constrained SVG, but the hardware compiler must never
    silently discard text, fills, masks, transforms, or unsupported shapes.
    """

    payload = path.read_bytes()
    issues: list[SvgIssue] = []
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        # Do not hand a declaration/entity payload to ElementTree merely to
        # produce a richer diagnostic: entity expansion happens while parsing.
        return SvgPreflight(
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_bytes=len(payload),
            width_mm=0.0,
            height_mm=0.0,
            path_count=0,
            layer_count=0,
            metadata_complete=False,
            issues=(
                SvgIssue(
                    "error",
                    "unsafe-xml-declaration",
                    "DOCTYPE and ENTITY declarations are not accepted in plot jobs.",
                ),
            ),
        )
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        return SvgPreflight(
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_bytes=len(payload),
            width_mm=0.0,
            height_mm=0.0,
            path_count=0,
            layer_count=0,
            metadata_complete=False,
            issues=(SvgIssue("error", "invalid-xml", str(exc)),),
        )
    if _local_name(root.tag) != "svg":
        issues.append(SvgIssue("error", "not-svg", "Root element is not <svg>."))

    parsed_width, width_unit = _strict_physical_length_mm(root.get("width"))
    parsed_height, height_unit = _strict_physical_length_mm(root.get("height"))
    width = parsed_width or 0.0
    height = parsed_height or 0.0
    dimensions_valid = parsed_width is not None and parsed_height is not None
    if not dimensions_valid:
        issues.append(
            SvgIssue(
                "error",
                "invalid-page-size",
                "SVG width and height must be positive, finite physical lengths with units.",
            )
        )
    if dimensions_valid and (width_unit != "mm" or height_unit != "mm"):
        issues.append(
            SvgIssue(
                "warning",
                "converted-page-units",
                "Page units were converted to millimetres; native plot plates use mm.",
            )
        )

    view_box = root.get("viewBox")
    if not view_box:
        issues.append(
            SvgIssue(
                "error",
                "missing-viewbox",
                "Hardware jobs require an explicit viewBox in millimetre coordinates.",
            )
        )
    else:
        try:
            values = [float(value) for value in re.split(r"[\s,]+", view_box.strip())]
        except ValueError:
            values = []
        if (
            len(values) != 4
            or not all(math.isfinite(value) for value in values)
            or values[2] <= 0
            or values[3] <= 0
        ):
            issues.append(
                SvgIssue("error", "invalid-viewbox", "SVG viewBox is invalid.")
            )
        elif dimensions_valid and any(
            abs(actual - expected) > 1e-6
            for actual, expected in zip(values, (0.0, 0.0, width, height))
        ):
            issues.append(
                SvgIssue(
                    "error",
                    "non-millimetre-viewbox",
                    "Hardware jobs require viewBox='0 0 WIDTH HEIGHT' with one unit per mm.",
                )
            )

    layers = [
        element
        for element in list(root)
        if _local_name(element.tag) == "g"
        and element.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer"
    ]
    if not layers:
        issues.append(
            SvgIssue(
                "error",
                "missing-physical-layers",
                "No top-level Inkscape plot layers were found.",
            )
        )
    metadata_complete = bool(layers) and all(
        (layer.get("data-plot-pen-id") or "").strip()
        and (layer.get("data-plot-nib-mm") or "").strip()
        and (layer.get("data-plot-ink") or "").strip()
        for layer in layers
    )
    if layers and not metadata_complete:
        issues.append(
            SvgIssue(
                "error",
                "incomplete-physical-metadata",
                "Some layers lack data-plot pen identity, ink, or nib metadata.",
            )
        )
    pen_declarations: dict[str, tuple[str, float, float, str]] = {}
    for layer in layers:
        pen_id = (layer.get("data-plot-pen-id") or "").strip()
        ink = (layer.get("data-plot-ink") or "").strip()
        raw_nib = layer.get("data-plot-nib-mm")
        raw_nominal = layer.get("data-plot-nominal-nib-mm")
        nib = _number(raw_nib)
        parsed_nominal = _number(raw_nominal)
        nominal = parsed_nominal
        if nominal is None:
            nominal = nib
        calibration = (
            layer.get("data-plot-calibration-state") or "nominal-unmeasured"
        ).strip()
        if pen_id and _PEN_ID.fullmatch(pen_id) is None:
            issues.append(
                SvgIssue(
                    "error",
                    "invalid-pen-metadata",
                    "data-plot-pen-id contains unsupported characters.",
                    layer.get("id"),
                )
            )
        if raw_nib is not None and (nib is None or not math.isfinite(nib) or nib <= 0):
            issues.append(
                SvgIssue(
                    "error",
                    "invalid-pen-metadata",
                    "data-plot-nib-mm must be positive and finite.",
                    layer.get("id"),
                )
            )
        if raw_nominal is not None and (
            parsed_nominal is None
            or not math.isfinite(parsed_nominal)
            or parsed_nominal <= 0
        ):
            issues.append(
                SvgIssue(
                    "error",
                    "invalid-pen-metadata",
                    "data-plot-nominal-nib-mm must be positive and finite.",
                    layer.get("id"),
                )
            )
        if pen_id and ink and nib is not None and nominal is not None:
            declaration = (ink, nib, nominal, calibration)
            previous = pen_declarations.setdefault(pen_id, declaration)
            if previous != declaration:
                issues.append(
                    SvgIssue(
                        "error",
                        "inconsistent-pen-id",
                        f"Pen id {pen_id!r} has conflicting physical metadata.",
                        layer.get("id"),
                    )
                )

    parents = {child: parent for parent in root.iter() for child in list(parent)}

    def inside_defs(element: ET.Element) -> bool:
        cursor = parents.get(element)
        while cursor is not None:
            if _local_name(cursor.tag) == "defs":
                return True
            cursor = parents.get(cursor)
        return False

    physical_paths = {
        path_element
        for layer in layers
        for path_element in layer.findall(f".//{{{SVG_NS}}}path")
        if not inside_defs(path_element)
    }

    def inherited_paint(element: ET.Element, name: str, fallback: str) -> str:
        cursor: ET.Element | None = element
        while cursor is not None:
            direct = cursor.get(name)
            style = cursor.get("style") or ""
            styled = re.search(
                rf"(?:^|;)\s*{re.escape(name)}\s*:\s*([^;]+)", style, re.I
            )
            value = styled.group(1) if styled else direct
            if value is not None and value.strip().casefold() != "inherit":
                return value.strip().casefold()
            cursor = parents.get(cursor)
        return fallback

    for layer in layers:
        for path_element in layer.findall(f".//{{{SVG_NS}}}path"):
            if inside_defs(path_element):
                continue
            element_id = path_element.get("id")
            stroke_paint = inherited_paint(path_element, "stroke", "none")
            fill_paint = inherited_paint(path_element, "fill", "black")
            if stroke_paint in {"", "none", "transparent"}:
                issues.append(
                    SvgIssue(
                        "error",
                        "path-has-no-stroke",
                        "A physical plot path must have a visible inherited stroke.",
                        element_id,
                    )
                )
            if fill_paint not in {"", "none", "transparent"}:
                issues.append(
                    SvgIssue(
                        "error",
                        "fill-is-not-plotted",
                        "Visible SVG fills are not motor paths; convert them to explicit linework.",
                        element_id,
                    )
                )

    path_count = 0
    supported_containers = {
        "svg",
        "g",
        "defs",
        "metadata",
        "namedview",
        "title",
        "desc",
    }
    unsupported_drawables = {
        "circle",
        "ellipse",
        "line",
        "polygon",
        "polyline",
        "rect",
        "text",
        "image",
        "use",
    }

    def declared_style_value(element: ET.Element, property_name: str) -> str | None:
        style = element.get("style") or ""
        styled = re.search(
            rf"(?:^|;)\s*{re.escape(property_name)}\s*:\s*([^;]+)",
            style,
            re.I,
        )
        value = styled.group(1) if styled else element.get(property_name)
        return value.strip().casefold() if value is not None else None

    for element in root.iter():
        name = _local_name(element.tag)
        element_id = element.get("id")
        transform = declared_style_value(element, "transform")
        if transform and transform != "none":
            issues.append(
                SvgIssue(
                    "error",
                    "unbaked-transform",
                    "Element transforms must be baked into path coordinates.",
                    element_id,
                )
            )
        effects = [
            declared_style_value(element, property_name)
            for property_name in ("clip-path", "mask", "filter")
        ]
        if any(value and value != "none" for value in effects):
            issues.append(
                SvgIssue(
                    "error",
                    "unbaked-visual-effect",
                    "Clip paths, masks, and filters must be baked before plotting.",
                    element_id,
                )
            )
        dash = declared_style_value(element, "stroke-dasharray")
        markers = [
            declared_style_value(element, property_name)
            for property_name in ("marker-start", "marker-mid", "marker-end")
        ]
        if (dash and dash != "none") or any(
            value and value != "none" for value in markers
        ):
            issues.append(
                SvgIssue(
                    "error",
                    "unbaked-stroke-decoration",
                    "Dashes and SVG markers must be converted to explicit path linework.",
                    element_id,
                )
            )
        for property_name in ("opacity", "stroke-opacity"):
            opacity = declared_style_value(element, property_name)
            if opacity is None:
                continue
            try:
                numeric_opacity = float(opacity)
            except ValueError:
                numeric_opacity = -1.0
            if numeric_opacity not in {0.0, 1.0}:
                issues.append(
                    SvgIssue(
                        "error",
                        "partial-opacity",
                        "Partial SVG opacity cannot be reproduced by a physical pen.",
                        element_id,
                    )
                )
        if name == "path":
            if element in physical_paths:
                path_count += 1
            else:
                if not inside_defs(element):
                    issues.append(
                        SvgIssue(
                            "error",
                            "path-outside-physical-layer",
                            "Drawable paths must belong to a top-level physical plot layer.",
                            element_id,
                        )
                    )
            # Exclude e/E because they may be the exponent marker in a valid
            # number such as 1e-3, rather than an SVG command.
            commands = set(re.findall(r"[A-DF-Za-df-z]", element.get("d", "")))
            unsupported = sorted(commands - set("MLCZmlczHhVv"))
            if unsupported:
                issues.append(
                    SvgIssue(
                        "error",
                        "unsupported-path-command",
                        f"Path uses unsupported command(s): {', '.join(unsupported)}.",
                        element_id,
                    )
                )
            else:
                syntax_error = _supported_path_syntax_error(element.get("d", ""))
                if syntax_error:
                    issues.append(
                        SvgIssue(
                            "error",
                            "invalid-path-data",
                            syntax_error,
                            element_id,
                        )
                    )
                elif any(
                    abs(float(match.group(2))) > max(width, height, 1.0) * 1000.0
                    for match in _TOKEN.finditer(element.get("d", ""))
                    if match.group(2)
                ):
                    issues.append(
                        SvgIssue(
                            "error",
                            "unreasonable-coordinate",
                            "Path coordinate magnitude is unreasonable for the physical page.",
                            element_id,
                        )
                    )
        elif name in unsupported_drawables:
            issues.append(
                SvgIssue(
                    "error",
                    "unsupported-drawable",
                    f"<{name}> must be converted to paths before hardware plotting.",
                    element_id,
                )
            )
        elif name not in supported_containers and not name.startswith("RDF"):
            # Namespaced editor metadata is harmless; unknown SVG drawing
            # elements are not, so retain a visible warning for the operator.
            namespace = element.tag.split("}", 1)[0] if "}" in element.tag else ""
            if namespace in {"", f"{{{SVG_NS}"}:
                issues.append(
                    SvgIssue(
                        "error",
                        "unknown-svg-element",
                        f"<{name}> is ignored by the plot compiler and must be removed or baked.",
                        element_id,
                    )
                )

    if path_count == 0:
        issues.append(SvgIssue("error", "no-paths", "SVG contains no path geometry."))
    direct_fit = machine is None or (
        width <= machine.work_width_mm + 1e-6
        and height <= machine.work_height_mm + 1e-6
    )
    rotated_fit = (
        machine is not None
        and machine.allow_page_rotation
        and (
            height <= machine.work_width_mm + 1e-6
            and width <= machine.work_height_mm + 1e-6
        )
    )
    if machine is not None and not direct_fit and not rotated_fit:
        issues.append(
            SvgIssue(
                "error",
                "page-exceeds-work-area",
                f"{width:g} x {height:g} mm page exceeds the machine's "
                f"{machine.work_width_mm:g} x {machine.work_height_mm:g} mm work area.",
            )
        )

    # Identical messages from repeated metadata nodes are noise, not evidence.
    unique: list[SvgIssue] = []
    seen_issues: set[tuple[str, str, str, str | None]] = set()
    for issue in issues:
        key = (issue.severity, issue.code, issue.message, issue.element)
        if key not in seen_issues:
            seen_issues.add(key)
            unique.append(issue)
    return SvgPreflight(
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_bytes=len(payload),
        width_mm=width,
        height_mm=height,
        path_count=path_count,
        layer_count=len(layers),
        metadata_complete=metadata_complete,
        issues=tuple(unique),
    )


# ---------------------------------------------------------------------------
# SVG -> strokes
# ---------------------------------------------------------------------------
def _flatten_cubic(p0, p1, p2, p3, tolerance: float) -> list[tuple[float, float]]:
    def midpoint(a, b):
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    def point_segment_distance(point, start, end):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length_squared = dx * dx + dy * dy
        if length_squared <= 1e-24:
            return math.dist(point, start)
        position = max(
            0.0,
            min(
                1.0,
                ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
                / length_squared,
            ),
        )
        nearest = (start[0] + position * dx, start[1] + position * dy)
        return math.dist(point, nearest)

    # De Casteljau subdivision makes ``curve_flatness_mm`` a real geometric
    # bound rather than a step-count hint. A cubic lies in its control-point
    # hull, so accepting only when both controls are within the tolerance of
    # the chord bounds the curve-to-polyline deviation by that tolerance.
    maximum_depth = 20
    stack = [(p0, p1, p2, p3, 0)]
    out: list[tuple[float, float]] = []
    while stack:
        a, b, c, d, depth = stack.pop()
        flat = (
            max(
                point_segment_distance(b, a, d),
                point_segment_distance(c, a, d),
            )
            <= tolerance
        )
        if flat:
            out.append(d)
            continue
        if depth >= maximum_depth:
            raise ValueError(
                "cubic exceeds the safe adaptive-flattening subdivision limit"
            )
        ab, bc, cd = midpoint(a, b), midpoint(b, c), midpoint(c, d)
        abc, bcd = midpoint(ab, bc), midpoint(bc, cd)
        centre = midpoint(abc, bcd)
        stack.append((centre, bcd, cd, d, depth + 1))
        stack.append((a, ab, abc, centre, depth + 1))
    return out


def flatten_path(d: str, tolerance: float) -> list[list[tuple[float, float]]]:
    commands: list[tuple[str, list[float]]] = []
    active = ""
    numbers: list[float] = []
    for match in _TOKEN.finditer(d):
        if match.group(1):
            if active:
                commands.append((active, numbers))
            active, numbers = match.group(1), []
        else:
            value = float(match.group(2))
            if not math.isfinite(value):
                raise ValueError("path coordinates must be finite numbers")
            numbers.append(value)
    if active:
        commands.append((active, numbers))

    subpaths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    cursor = (0.0, 0.0)
    origin = (0.0, 0.0)
    for letter, values in commands:
        upper = letter.upper()
        relative = letter.islower()
        if upper == "Z":
            if current and current[0] != current[-1]:
                current.append(current[0])
            cursor = current[0] if current else cursor
            continue
        arity = {"M": 2, "L": 2, "C": 6, "H": 1, "V": 1}.get(upper, 0)
        if arity == 0:
            continue
        for offset in range(0, len(values) - arity + 1, arity):
            chunk = values[offset : offset + arity]
            if upper == "H":
                target = (cursor[0] + chunk[0] if relative else chunk[0], cursor[1])
            elif upper == "V":
                target = (cursor[0], cursor[1] + chunk[0] if relative else chunk[0])
            elif upper == "C":
                base = cursor if relative else (0.0, 0.0)
                c1 = (
                    (base[0] + chunk[0], base[1] + chunk[1])
                    if relative
                    else (chunk[0], chunk[1])
                )
                c2 = (
                    (base[0] + chunk[2], base[1] + chunk[3])
                    if relative
                    else (chunk[2], chunk[3])
                )
                target = (
                    (base[0] + chunk[4], base[1] + chunk[5])
                    if relative
                    else (chunk[4], chunk[5])
                )
                current.extend(_flatten_cubic(cursor, c1, c2, target, tolerance))
                cursor = target
                continue
            else:
                target = (
                    (cursor[0] + chunk[0], cursor[1] + chunk[1])
                    if relative
                    else (chunk[0], chunk[1])
                )
            if upper == "M" and offset == 0:
                if len(current) >= 2:
                    subpaths.append(current)
                current = [target]
                origin = target
            else:
                current.append(target)
            cursor = target
    if len(current) >= 2:
        subpaths.append(current)
    _ = origin
    return subpaths


def _nib_from_label(label: str) -> float:
    """Last-resort nib for a plate with no plot metadata (pre-spec output)."""

    match = re.search(r"(\d+(?:[.,]\d+)?)\s*$", label.strip())
    return float(match.group(1).replace(",", ".")) if match else 0.3


def _pen_from_label(label: str) -> str:
    return label.split("—")[-1].strip() if "—" in label else label.strip()


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except (AttributeError, ValueError):
        return None


def _count(value: str | None, fallback: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 1 else fallback


def _layer_pen(group: ET.Element, label: str) -> tuple[Pen, dict]:
    """Resolve the pen a layer really loads from its machine-readable plan.

    The Inkscape label is a human caption: it carries the *nominal* barrel
    size, and only for as long as nobody renames a layer.  The plate already
    states the physical plan, so read that and keep the label for display.
    """

    ink = (group.get("data-plot-ink") or "").strip()
    effective = _number(group.get("data-plot-nib-mm"))
    nominal = _number(group.get("data-plot-nominal-nib-mm"))
    declared = {
        "strokes": _count(group.get("data-plot-strokes")),
        "passes": _count(group.get("data-plot-passes")),
        "width_mm": _number(group.get("data-plot-width-mm")),
        "pitch_mm": _number(group.get("data-plot-offset-pitch-mm")) or 0.0,
        "mode": (group.get("data-plot-width-fit-mode") or "").strip(),
    }
    if effective is None and nominal is None:
        # Pre-specification plate: nothing to read but the caption.
        nominal = effective = _nib_from_label(label)
        metadata = False
    else:
        metadata = True
        if effective is None:
            effective = nominal
        if nominal is None:
            nominal = effective
    assert effective is not None and nominal is not None
    if (
        not math.isfinite(effective)
        or effective <= 0
        or not math.isfinite(nominal)
        or nominal <= 0
    ):
        raise ValueError(f"layer {label!r} has invalid physical nib metadata")
    calibration = (
        group.get("data-plot-calibration-state") or "nominal-unmeasured"
    ).strip()
    pen_id = (group.get("data-plot-pen-id") or "").strip()
    if not pen_id:
        pen_id = f"{ink.casefold() or 'pen'}-{nominal:g}".replace(".", "-")
    display = f"{ink} {nominal:g}".strip() if ink else _pen_from_label(label)
    if calibration == "measured" and abs(effective - nominal) > 5e-4:
        # A measured pen that does not mark its barrel size is a different pen
        # on paper; say so rather than quietly drawing the nominal width.
        display = f"{display} ({effective:g} eff)"
    declared["metadata"] = metadata
    return (
        Pen(
            id=pen_id,
            ink=ink,
            nib_mm=effective,
            nominal_mm=nominal,
            colour=group.get("stroke") or "#222222",
            label=display,
            calibration=calibration,
        ),
        declared,
    )


def load_plate(path: Path, machine: Machine) -> tuple[list[Stroke], dict]:
    root = ET.parse(path).getroot()
    parents = {child: parent for parent in root.iter() for child in list(parent)}

    def _hidden(element: ET.Element) -> bool:
        cursor: ET.Element | None = element
        while cursor is not None:
            if (cursor.get("display") or "").strip().casefold() == "none":
                return True
            if (cursor.get("visibility") or "").strip().casefold() in {
                "hidden",
                "collapse",
            }:
                return True
            style = cursor.get("style") or ""
            if re.search(r"(?:^|;)\s*display\s*:\s*none(?:;|$)", style, re.I):
                return True
            if re.search(
                r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)(?:;|$)",
                style,
                re.I,
            ):
                return True
            for property_name in ("opacity", "stroke-opacity"):
                opacity = cursor.get(property_name)
                styled_opacity = re.search(
                    rf"(?:^|;)\s*{property_name}\s*:\s*([^;]+)", style, re.I
                )
                opacity = styled_opacity.group(1) if styled_opacity else opacity
                try:
                    if opacity is not None and float(opacity.strip()) <= 0:
                        return True
                except ValueError:
                    pass
            cursor = parents.get(cursor)
        return False

    width, _width_unit = _physical_length_mm(root.get("width"), 210.0)
    height, _height_unit = _physical_length_mm(root.get("height"), 297.0)

    page = {
        "width": width,
        "height": height,
        "title": (root.findtext(f"{{{SVG_NS}}}title") or path.stem).strip(),
    }

    strokes: list[Stroke] = []
    layers: list[dict] = []
    for group in root.findall(f"{{{SVG_NS}}}g"):
        if group.get(f"{{{INKSCAPE_NS}}}groupmode") != "layer":
            continue
        if _hidden(group):
            continue
        label = group.get(f"{{{INKSCAPE_NS}}}label") or group.get("id") or "layer"
        pen, declared = _layer_pen(group, label)
        first = len(strokes)
        for element in group.findall(f".//{{{SVG_NS}}}path"):
            cursor = parents.get(element)
            defined_only = False
            while cursor is not None and cursor is not group:
                if _local_name(cursor.tag) == "defs":
                    defined_only = True
                    break
                cursor = parents.get(cursor)
            if defined_only:
                continue
            if _hidden(element):
                continue
            try:
                subpaths = flatten_path(element.get("d", ""), machine.curve_flatness_mm)
            except ValueError as exc:
                identity = element.get("id") or "unnamed path"
                raise ValueError(f"cannot flatten {identity}: {exc}") from exc
            for points in subpaths:
                # This six-decimal millimetre representation is the shared
                # simulator/job/controller geometry.  Hardware exporters must
                # never re-flatten or silently move a point after timing it.
                physical: list[tuple[float, float]] = []
                for x, y in points:
                    point = (round(x, 6), round(y, 6))
                    if not physical or point != physical[-1]:
                        physical.append(point)
                if len(physical) >= 2:
                    strokes.append(Stroke(label, pen, physical, len(strokes)))
        layers.append(
            {
                "label": label,
                "pen": pen,
                "declared": declared,
                "subpaths": len(strokes) - first,
                "length_mm": sum(_length(stroke.points) for stroke in strokes[first:]),
            }
        )
    page["layers"] = layers
    page["metadata"] = bool(layers) and all(
        layer["declared"]["metadata"] for layer in layers
    )
    return strokes, page


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def _pen_groups(strokes: list[Stroke]) -> list[tuple[Pen, list[Stroke]]]:
    """Run-length groups in document order, then merge identical pens.

    Merging is what a competent driver does: it plots every layer sharing a pen
    before asking for a swap.  Identity is the plate's own pen id, so two
    differently calibrated pens of the same barrel size stay distinct loads.
    """
    groups: list[tuple[Pen, list[Stroke]]] = []
    for stroke in strokes:
        if groups and groups[-1][0].id == stroke.pen.id:
            groups[-1][1].append(stroke)
        else:
            groups.append((stroke.pen, [stroke]))
    return groups


def _merge_pens(
    groups: list[tuple[Pen, list[Stroke]]],
) -> list[tuple[Pen, list[Stroke]]]:
    merged: dict[str, list[Stroke]] = {}
    order: list[Pen] = []
    for pen, items in groups:
        if pen.id not in merged:
            merged[pen.id] = []
            order.append(pen)
        merged[pen.id].extend(items)
    return [(pen, merged[pen.id]) for pen in order]


def _nearest_neighbour(strokes: list[Stroke], start=(0.0, 0.0)) -> list[Stroke]:
    """Greedy nearest-neighbour with per-stroke reversal.

    Small jobs use the obvious scan.  Large map plates use an exact spatial
    grid over both stroke endpoints, avoiding the quadratic scan that made a
    50,000-stroke SVG needlessly expensive while retaining deterministic ties.
    """

    if len(strokes) >= 512:
        return _nearest_neighbour_grid(strokes, start)
    remaining = list(strokes)
    ordered: list[Stroke] = []
    cursor = start
    while remaining:
        best_index, best_cost, best_flip = 0, float("inf"), False
        for index, stroke in enumerate(remaining):
            forward = math.dist(cursor, stroke.start)
            backward = math.dist(cursor, stroke.end)
            if forward < best_cost:
                best_index, best_cost, best_flip = index, forward, False
            if backward < best_cost:
                best_index, best_cost, best_flip = index, backward, True
        chosen = remaining.pop(best_index)
        if best_flip:
            chosen = chosen.reversed_copy()
        ordered.append(chosen)
        cursor = chosen.end
    return ordered


def _nearest_neighbour_grid(
    strokes: list[Stroke], start: tuple[float, float]
) -> list[Stroke]:
    endpoints = [point for stroke in strokes for point in (stroke.start, stroke.end)]
    min_x = min(point[0] for point in endpoints)
    min_y = min(point[1] for point in endpoints)
    max_x = max(point[0] for point in endpoints)
    max_y = max(point[1] for point in endpoints)
    diagonal = math.hypot(max_x - min_x, max_y - min_y)
    cell = max(0.5, diagonal / max(math.sqrt(len(strokes)), 1.0))

    def cell_at(point: tuple[float, float]) -> tuple[int, int]:
        return (
            math.floor((point[0] - min_x) / cell),
            math.floor((point[1] - min_y) / cell),
        )

    buckets: dict[tuple[int, int], list[tuple[int, bool, tuple[float, float]]]] = {}
    for index, stroke in enumerate(strokes):
        buckets.setdefault(cell_at(stroke.start), []).append(
            (index, False, stroke.start)
        )
        buckets.setdefault(cell_at(stroke.end), []).append((index, True, stroke.end))
    occupied_x = [key[0] for key in buckets]
    occupied_y = [key[1] for key in buckets]
    grid_min_x, grid_max_x = min(occupied_x), max(occupied_x)
    grid_min_y, grid_max_y = min(occupied_y), max(occupied_y)

    active = [True] * len(strokes)
    ordered: list[Stroke] = []
    cursor = start
    for _position in range(len(strokes)):
        centre_x, centre_y = cell_at(cursor)
        max_ring = max(
            abs(centre_x - grid_min_x),
            abs(centre_x - grid_max_x),
            abs(centre_y - grid_min_y),
            abs(centre_y - grid_max_y),
        )
        best: tuple[float, int, bool] | None = None
        for ring in range(max_ring + 1):
            cells: list[tuple[int, int]] = []
            if ring == 0:
                cells.append((centre_x, centre_y))
            else:
                left, right = centre_x - ring, centre_x + ring
                top, bottom = centre_y - ring, centre_y + ring
                cells.extend((x, top) for x in range(left, right + 1))
                cells.extend((x, bottom) for x in range(left, right + 1))
                cells.extend((left, y) for y in range(top + 1, bottom))
                cells.extend((right, y) for y in range(top + 1, bottom))
            for key in cells:
                for index, flip, point in buckets.get(key, ()):
                    if not active[index]:
                        continue
                    candidate = (math.dist(cursor, point), index, flip)
                    if best is None or candidate < best:
                        best = candidate
            if best is not None:
                left_edge = min_x + (centre_x - ring) * cell
                right_edge = min_x + (centre_x + ring + 1) * cell
                top_edge = min_y + (centre_y - ring) * cell
                bottom_edge = min_y + (centre_y + ring + 1) * cell
                unseen_floor = min(
                    cursor[0] - left_edge,
                    right_edge - cursor[0],
                    cursor[1] - top_edge,
                    bottom_edge - cursor[1],
                )
                if best[0] <= unseen_floor + 1e-12:
                    break
        if best is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("nearest-neighbour endpoint index lost an active stroke")
        _distance, index, flip = best
        active[index] = False
        chosen = strokes[index].reversed_copy() if flip else strokes[index]
        ordered.append(chosen)
        cursor = chosen.end
    return ordered


def order_strokes(strokes: list[Stroke], mode: str) -> list[tuple[Pen, list[Stroke]]]:
    groups = _pen_groups(strokes)
    if mode == "document":
        return groups
    groups = _merge_pens(groups)
    if mode == "merged":
        return groups
    cursor = (0.0, 0.0)
    optimised: list[tuple[Pen, list[Stroke]]] = []
    for pen, items in groups:
        ordered = _nearest_neighbour(items, cursor)
        cursor = ordered[-1].end if ordered else cursor
        optimised.append((pen, ordered))
    return optimised


# ---------------------------------------------------------------------------
# Motion planner
# ---------------------------------------------------------------------------
def plan_polyline(
    points: list[tuple[float, float]],
    v_max: float,
    accel: float,
    tolerance: float,
    *,
    grbl_cartesian: bool = False,
) -> list[float]:
    """Cumulative time at each vertex under a trapezoidal profile.

    Junction speed uses the standard deviation model: the tighter the corner,
    the lower the speed that keeps the machine within `tolerance` of the vertex.
    """
    count = len(points)
    if count < 2:
        return [0.0]
    lengths = [math.dist(points[i], points[i + 1]) for i in range(count - 1)]

    def direction(
        start: tuple[float, float], end: tuple[float, float]
    ) -> tuple[float, float]:
        length = math.dist(start, end)
        if length <= 1e-12:
            return (0.0, 0.0)
        return ((end[0] - start[0]) / length, (end[1] - start[1]) / length)

    def cartesian_limit(vector: tuple[float, float]) -> float:
        if not grbl_cartesian:
            return accel
        limits = [
            accel / abs(component) for component in vector if abs(component) > 1e-12
        ]
        return min(limits, default=accel)

    directions = [direction(points[i], points[i + 1]) for i in range(count - 1)]
    accelerations = [cartesian_limit(vector) for vector in directions]

    speeds = [0.0] * count
    for i in range(1, count - 1):
        previous = directions[i - 1]
        following = directions[i]
        if previous == (0.0, 0.0) or following == (0.0, 0.0):
            speeds[i] = v_max
            continue
        cosine = max(
            -1.0,
            min(1.0, previous[0] * following[0] + previous[1] * following[1]),
        )
        if cosine > 1 - 1e-9:
            speeds[i] = v_max
        elif cosine < -1 + 1e-9:
            speeds[i] = 0.0
        else:
            # GRBL defines theta between the reverse of the incoming vector and
            # the outgoing vector. Its sin(theta/2) is therefore cos(turn/2),
            # not sin(turn/2). Using the latter makes shallow bends slower than
            # sharp corners and is physically backwards.
            sin_half = math.sqrt(max(0.0, (1.0 + cosine) / 2.0))
            junction_vector = (
                following[0] - previous[0],
                following[1] - previous[1],
            )
            junction_length = math.hypot(*junction_vector)
            junction_direction = (
                junction_vector[0] / junction_length,
                junction_vector[1] / junction_length,
            )
            junction_accel = cartesian_limit(junction_direction)
            speeds[i] = min(
                v_max,
                math.sqrt(junction_accel * tolerance * sin_half / (1.0 - sin_half)),
            )

    # backward then forward acceleration-limit passes
    for i in range(count - 2, -1, -1):
        speeds[i] = min(
            speeds[i],
            math.sqrt(speeds[i + 1] ** 2 + 2 * accelerations[i] * lengths[i]),
        )
    for i in range(count - 1):
        speeds[i + 1] = min(
            speeds[i + 1],
            math.sqrt(speeds[i] ** 2 + 2 * accelerations[i] * lengths[i]),
        )

    cumulative = [0.0]
    for i, length in enumerate(lengths):
        v0, v1 = speeds[i], speeds[i + 1]
        segment_accel = accelerations[i]
        if length <= 1e-12:
            cumulative.append(cumulative[-1])
            continue
        peak = min(
            v_max,
            math.sqrt(
                max(
                    (2 * segment_accel * length + v0 * v0 + v1 * v1) / 2,
                    0.0,
                )
            ),
        )
        d_acc = max((peak * peak - v0 * v0) / (2 * segment_accel), 0.0)
        d_dec = max((peak * peak - v1 * v1) / (2 * segment_accel), 0.0)
        cruise = max(length - d_acc - d_dec, 0.0)
        time = (
            (peak - v0) / segment_accel
            + (peak - v1) / segment_accel
            + (cruise / peak if peak > 1e-9 else 0.0)
        )
        cumulative.append(cumulative[-1] + time)
    return cumulative


def simulate(
    groups: list[tuple[Pen, list[Stroke]]], machine: Machine, home=(0.0, 0.0)
) -> tuple[list[Move], dict]:
    moves: list[Move] = []
    clock = 0.0
    cursor = home
    pen_loads = 0
    pen_swaps = 0

    for index, (pen, strokes) in enumerate(groups):
        if not strokes:
            continue
        # The first pen is already in the holder when the job starts; only
        # subsequent swaps cost the operator time.
        swap = 0.0 if index == 0 else machine.pen_change_s
        moves.append(
            Move("penchange", pen, strokes[0].layer, [cursor], clock, swap, [0.0, swap])
        )
        clock += swap
        # Every per-pen hardware program begins by commanding pen-up and
        # waiting for the calibrated lift time before its first travel.
        clock += machine.pen_lift_s
        pen_loads += 1
        if index > 0:
            pen_swaps += 1

        for stroke in strokes:
            travel_points = [cursor, stroke.start]
            if math.dist(cursor, stroke.start) > 1e-9:
                cumulative = plan_polyline(
                    travel_points,
                    machine.pen_up_speed_mm_s,
                    machine.acceleration_mm_s2,
                    machine.cornering_tolerance_mm,
                    grbl_cartesian=machine.motion_model == "grbl-cartesian",
                )
                cumulative = [value * machine.timing_scale for value in cumulative]
                duration = cumulative[-1] + machine.command_latency_s
                moves.append(
                    Move(
                        "travel",
                        stroke.pen,
                        stroke.layer,
                        travel_points,
                        clock,
                        duration,
                        cumulative,
                    )
                )
                clock += duration
            clock += machine.pen_lower_s
            cumulative = plan_polyline(
                stroke.points,
                machine.pen_down_speed_mm_s,
                machine.acceleration_mm_s2,
                machine.cornering_tolerance_mm,
                grbl_cartesian=machine.motion_model == "grbl-cartesian",
            )
            cumulative = [value * machine.timing_scale for value in cumulative]
            duration = cumulative[-1] + machine.command_latency_s
            moves.append(
                Move(
                    "draw",
                    stroke.pen,
                    stroke.layer,
                    stroke.points,
                    clock,
                    duration,
                    cumulative,
                )
            )
            clock += duration + machine.pen_lift_s
            cursor = stroke.end

    draw_distance = sum(_length(m.points) for m in moves if m.kind == "draw")
    travel_distance = sum(_length(m.points) for m in moves if m.kind == "travel")
    # Ink laid down is a per-pen product: a 1.00 nib empties a cartridge in a
    # fraction of the distance a 0.25 does, so a single total would hide the
    # whole point of a paper-aware nib ladder.
    ink_mm2 = sum(
        _length(move.points) * move.nib_mm for move in moves if move.kind == "draw"
    )
    all_points = [point for move in moves for point in move.points]
    bounds = (
        {
            "min_x_mm": min(point[0] for point in all_points),
            "min_y_mm": min(point[1] for point in all_points),
            "max_x_mm": max(point[0] for point in all_points),
            "max_y_mm": max(point[1] for point in all_points),
        }
        if all_points
        else {"min_x_mm": 0.0, "min_y_mm": 0.0, "max_x_mm": 0.0, "max_y_mm": 0.0}
    )
    uncertainty = machine.timing_uncertainty_fraction
    kinematic_seconds = sum(
        move.cumulative[-1]
        for move in moves
        if move.kind in {"draw", "travel"} and move.cumulative
    )
    command_latency_seconds = sum(
        max(0.0, move.duration - move.cumulative[-1])
        for move in moves
        if move.kind in {"draw", "travel"} and move.cumulative
    )
    servo_seconds = pen_loads * machine.pen_lift_s + sum(
        1 for move in moves if move.kind == "draw"
    ) * (machine.pen_lower_s + machine.pen_lift_s)
    manual_change_seconds = pen_swaps * machine.pen_change_s
    stats = {
        "machine": machine.name,
        "total_seconds": clock,
        "total_low_seconds": clock * max(0.0, 1.0 - uncertainty),
        "total_high_seconds": clock * (1.0 + uncertainty),
        "timing_uncertainty_fraction": uncertainty,
        "calibration_state": machine.calibration_state,
        # ``pen_changes`` historically meant loads in plotsim output. Retain
        # it for report compatibility while naming both quantities exactly.
        "pen_changes": pen_loads,
        "pen_loads": pen_loads,
        "pen_swaps": pen_swaps,
        "kinematic_seconds": kinematic_seconds,
        "command_latency_seconds": command_latency_seconds,
        "servo_seconds": servo_seconds,
        "manual_change_seconds": manual_change_seconds,
        "pen_down_mm": draw_distance,
        "pen_up_mm": travel_distance,
        "pen_lifts": sum(1 for m in moves if m.kind == "draw"),
        "travel_ratio": travel_distance / draw_distance if draw_distance else 0.0,
        "ink_mm2": ink_mm2,
        "bounds": bounds,
    }
    return moves, stats


def _length(points: list[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def build_timeline(
    moves: list[Move], page: dict, stats: dict, machine: Machine
) -> dict:
    pens: list[dict] = []
    seen: dict[str, int] = {}
    for move in moves:
        if move.pen.id not in seen:
            seen[move.pen.id] = len(pens)
            pens.append(
                {
                    "pen": move.pen.label,
                    "id": move.pen.id,
                    "ink": move.pen.ink,
                    "colour": move.pen.colour,
                    "nib_mm": move.pen.nib_mm,
                    "nominal_nib_mm": move.pen.nominal_mm,
                    "calibration": move.pen.calibration,
                }
            )
    return {
        "schema": "plotsim-2",
        "page": {
            "width": page["width"],
            "height": page["height"],
            "title": page["title"],
            "metadata": page.get("metadata", False),
            "layers": [
                {
                    "label": layer["label"],
                    "pen": layer["pen"].id,
                    "nib_mm": layer["pen"].nib_mm,
                    "width_mm": layer["declared"]["width_mm"],
                    "strokes": layer["declared"]["strokes"],
                    "passes": layer["declared"]["passes"],
                    "pitch_mm": layer["declared"]["pitch_mm"],
                    "mode": layer["declared"]["mode"],
                    "subpaths": layer["subpaths"],
                    "length_mm": round(layer["length_mm"], 2),
                }
                for layer in page.get("layers", [])
            ],
        },
        "machine": machine.as_dict(),
        "pens": pens,
        "stats": stats,
        "moves": [
            {
                "k": move.kind[0],  # d | t | p
                "p": seen[move.pen.id],
                "t": round(move.t_start, 4),
                "d": round(move.duration, 4),
                "pts": [[round(x, 2), round(y, 2)] for x, y in move.points],
                "ct": [round(value, 4) for value in move.cumulative],
            }
            for move in moves
        ],
    }


def _format_time(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"
    )


def _report(label: str, stats: dict) -> None:
    print(
        f"  {label:<12} {_format_time(stats['total_seconds']):>12}"
        f"  down {stats['pen_down_mm'] / 1000:7.2f} m"
        f"  up {stats['pen_up_mm'] / 1000:7.2f} m"
        f"  ratio {stats['travel_ratio']:5.2f}x"
        f"  lifts {stats['pen_lifts']:5d}"
        f"  pens {stats['pen_changes']:2d}"
        f"  ink {stats['ink_mm2']:8.0f} mm²"
    )


def _report_layers(page: dict) -> None:
    """Print the physical plan each layer declares, next to its geometry.

    A declared stroke count that the geometry does not contain is the one
    failure this tool exists to catch: it means the plate promises a wider
    mark than the machine will actually draw.
    """

    layers = page.get("layers") or []
    if not layers:
        return
    if not page.get("metadata", False):
        print(
            "  ! no data-plot-* metadata on some layers — nib widths fell back "
            "to the layer label\n"
        )
    print("  Physical plan (declared by the plate):")
    for layer in layers:
        declared = layer["declared"]
        pen = layer["pen"]
        width = declared["width_mm"]
        build = f"{pen.nib_mm:g}"
        if declared["strokes"] > 1:
            build = f"{pen.nib_mm:g} x{declared['strokes']} @ {declared['pitch_mm']:g}"
        if declared["passes"] > 1:
            build += f", {declared['passes']} passes"
        name = layer["label"]
        if len(name) > 46:
            name = name[:45] + "…"
        print(
            f"    {name:<46} {build:<24}"
            f" mark {width if width is not None else pen.nib_mm:>5g} mm"
            f"  {layer['subpaths']:6d} subpaths"
            f"  {layer['length_mm'] / 1000:7.2f} m"
        )
    print()


def _offset_geometry_warnings(page: dict) -> list[str]:
    """Report layers whose geometry cannot contain the offsets they declare."""

    problems: list[str] = []
    for layer in page.get("layers") or []:
        declared = layer["declared"]
        multiplicity = declared["strokes"] * declared["passes"]
        if multiplicity <= 1 or layer["subpaths"] == 0:
            continue
        if layer["subpaths"] < multiplicity:
            problems.append(
                f"layer {layer['label']!r} declares {declared['strokes']} offset "
                f"stroke(s) x {declared['passes']} pass(es) but carries only "
                f"{layer['subpaths']} subpath(s) — the plotted mark will be "
                "narrower than the plate claims"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", type=Path)
    parser.add_argument(
        "--order", choices=("document", "merged", "optimised"), default="optimised"
    )
    parser.add_argument("--json", type=Path, help="write the timeline for the viewer")
    parser.add_argument(
        "--compare", action="store_true", help="report all three orderings"
    )
    parser.add_argument(
        "--layers",
        action="store_true",
        help="report the physical plan each layer declares",
    )
    parser.add_argument("--machine-profile", type=Path)
    parser.add_argument("--pen-down-speed", type=float)
    parser.add_argument("--pen-up-speed", type=float)
    parser.add_argument("--acceleration", type=float)
    parser.add_argument("--pen-change-seconds", type=float)
    parser.add_argument("--pen-lift-seconds", type=float)
    parser.add_argument("--pen-lower-seconds", type=float)
    parser.add_argument("--cornering-tolerance", type=float)
    parser.add_argument("--curve-flatness", type=float)
    parser.add_argument("--timing-uncertainty", type=float)
    parser.add_argument(
        "--strict-svg",
        action="store_true",
        help="refuse SVG constructs that cannot be sent safely to hardware",
    )
    args = parser.parse_args(argv)

    try:
        base = (
            Machine.from_json(args.machine_profile)
            if args.machine_profile
            else Machine()
        )
        machine_values = base.as_dict()
        overrides = {
            "pen_down_speed_mm_s": args.pen_down_speed,
            "pen_up_speed_mm_s": args.pen_up_speed,
            "acceleration_mm_s2": args.acceleration,
            "pen_change_s": args.pen_change_seconds,
            "pen_lift_s": args.pen_lift_seconds,
            "pen_lower_s": args.pen_lower_seconds,
            "cornering_tolerance_mm": args.cornering_tolerance,
            "curve_flatness_mm": args.curve_flatness,
            "timing_uncertainty_fraction": args.timing_uncertainty,
        }
        machine_values.update(
            {key: value for key, value in overrides.items() if value is not None}
        )
        machine = Machine(**machine_values)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.svg.exists():
        print(f"no such file: {args.svg}", file=sys.stderr)
        return 2

    preflight = preflight_svg(args.svg, machine)
    for issue in preflight.issues:
        if issue.severity in {"error", "warning"}:
            location = f" [{issue.element}]" if issue.element else ""
            print(
                f"  {issue.severity.upper()}: {issue.code}{location}: {issue.message}",
                file=sys.stderr,
            )
    if args.strict_svg and preflight.errors:
        print("strict SVG preflight failed", file=sys.stderr)
        return 2

    try:
        strokes, page = load_plate(args.svg, machine)
    except ValueError as exc:
        print(f"SVG path geometry could not be compiled: {exc}", file=sys.stderr)
        return 2
    if not strokes:
        print("no plottable strokes found", file=sys.stderr)
        return 1

    print(
        f"{args.svg.name}  —  {page['title']}  ({page['width']:g} x {page['height']:g} mm)"
    )
    print(f"  {len(strokes)} strokes, {sum(len(s.points) for s in strokes)} vertices\n")

    if args.layers:
        _report_layers(page)

    if args.compare:
        for mode in ("document", "merged", "optimised"):
            _, stats = simulate(order_strokes(strokes, mode), machine)
            _report(mode, stats)
        print()

    groups = order_strokes(strokes, args.order)
    moves, stats = simulate(groups, machine)
    if not args.compare:
        _report(args.order, stats)
        print()

    print("  Pen sequence:")
    for step, (pen, items) in enumerate(groups, start=1):
        if items:
            distance = sum(_length(s.points) for s in items)
            ink = sum(_length(s.points) * s.nib_mm for s in items)
            print(
                f"    {step}. {pen.label:<20} {len(items):5d} strokes"
                f"  {distance / 1000:6.2f} m  {ink:8.0f} mm² ink"
            )

    for problem in _offset_geometry_warnings(page):
        print(f"\n  ! {problem}", file=sys.stderr)

    if args.json:
        timeline = build_timeline(moves, page, stats, machine)
        timeline["preflight"] = preflight.as_dict()
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(timeline, separators=(",", ":")), encoding="utf-8"
        )
        size = args.json.stat().st_size / 1024
        print(f"\n  wrote {args.json} ({size:.0f} KB, {len(moves)} moves)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
