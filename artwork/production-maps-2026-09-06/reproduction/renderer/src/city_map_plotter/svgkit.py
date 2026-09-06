"""Neutral SVG emission primitives shared by the map path and the furniture.

Nothing here decides *what* a plate shows.  These are the millimetre-exact
serialisation, physical-annotation, and manifest-record helpers that both
``svg.py`` (map linework) and ``furniture.py`` (everything else) must agree on,
kept in one place so the two cannot drift apart.
"""

from __future__ import annotations

import hashlib
import re
from math import hypot
from typing import Any, TypeAlias
from xml.etree import ElementTree as ET

from .geometry import polyline_length
from .models import MapPlotterError
from .pens import PenInventory, PenWidthFit, fit_pen_width, style_pen_width


Point: TypeAlias = tuple[float, float]
Stroke: TypeAlias = list[Point]

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
MAP_NS = "urn:city-map-plotter:metadata"
SVG_COORDINATE_DECIMALS = 3
SVG_COORDINATE_QUANTUM_MM = 10.0**-SVG_COORDINATE_DECIMALS
ET.register_namespace("", SVG_NS)
ET.register_namespace("inkscape", INKSCAPE_NS)
ET.register_namespace("sodipodi", SODIPODI_NS)
ET.register_namespace("mapplot", MAP_NS)


def svg_tag(tag: str) -> str:
    return f"{{{SVG_NS}}}{tag}"


def inkscape_attribute(name: str) -> str:
    return f"{{{INKSCAPE_NS}}}{name}"


def format_number(value: float) -> str:
    formatted = f"{value:.{SVG_COORDINATE_DECIMALS}f}".rstrip("0").rstrip(".")
    return formatted if formatted not in {"", "-0"} else "0"


def format_measurement(value: float) -> str:
    """Serialize calibrated physical measurements without moving path vertices."""

    formatted = f"{value:.6f}".rstrip("0").rstrip(".")
    return formatted if formatted not in {"", "-0"} else "0"


PATH_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def path_bounds(paths: list[ET.Element]) -> dict[str, float] | None:
    points: list[Point] = []
    for path in paths:
        numbers = [float(value) for value in PATH_NUMBER.findall(path.get("d", ""))]
        points.extend(zip(numbers[0::2], numbers[1::2]))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "x": round(min(xs), 3),
        "y": round(min(ys), 3),
        "width": round(max(xs) - min(xs), 3),
        "height": round(max(ys) - min(ys), 3),
    }


def path_geometry_sha256(paths: list[ET.Element]) -> str:
    """Hash ordered, already-serialized path geometry for copy integrity."""

    values: list[str] = []
    for path in paths:
        data = path.get("d")
        if not data:
            raise MapPlotterError("Typography path is missing serialized geometry.")
        values.append(data)
    return hashlib.sha256("\n".join(values).encode("ascii")).hexdigest()


def stroke_geometry_sha256(strokes: list[Stroke]) -> str:
    """Hash the canonical SVG geometry independently from an XML element."""

    values = [path_data(stroke) for stroke in strokes if len(stroke) >= 2]
    if not values:
        raise MapPlotterError("Typography source copy produced no drawable geometry.")
    return hashlib.sha256("\n".join(values).encode("ascii")).hexdigest()


PLOT_PATH_TAGS = (
    "ink",
    "pen-id",
    "nib-mm",
    "nominal-nib-mm",
    "calibration-state",
    "calibration-substrate",
    "plotted-width-mm",
    "requested-width-mm",
    "width-fit-error-mm",
    "width-fit-mode",
    "offset-pitch-mm",
    "pen-profile",
    "stroke-index",
    "stroke-count",
    "pass-index",
    "pass-count",
    "offset-fallback",
    "physical-conflict",
)


def plot_path_attributes(tags: dict[str, str]) -> dict[str, str]:
    """Serialize every physical path decision needed to audit the SVG."""

    return {
        f"data-plot-{tag_name}": tags[f"plot:{tag_name}"]
        for tag_name in PLOT_PATH_TAGS
        if f"plot:{tag_name}" in tags
    }


def linear_path_data(points: list[Point]) -> str:
    head, *tail = points
    chunks = [f"M {format_number(head[0])},{format_number(head[1])}"]
    chunks.extend(f"L {format_number(x)},{format_number(y)}" for x, y in tail)
    if len(points) > 3 and points[0] == points[-1]:
        chunks[-1] = "Z"
    return " ".join(chunks)


def clamped_tangent(dx: float, dy: float, maximum: float) -> tuple[float, float]:
    length = hypot(dx, dy)
    if length <= maximum or length <= 1e-12:
        return dx, dy
    factor = maximum / length
    return dx * factor, dy * factor


def smooth_path_data(
    points: list[Point],
    *,
    bounds: tuple[float, float, float, float] | None = None,
) -> str:
    """Convert a restrained Catmull-Rom fit into editable SVG cubic curves."""

    closed = len(points) > 3 and points[0] == points[-1]
    vertices = points[:-1] if closed else points
    if len(vertices) < 3:
        return linear_path_data(points)
    chunks = [f"M {format_number(vertices[0][0])},{format_number(vertices[0][1])}"]
    segment_count = len(vertices) if closed else len(vertices) - 1
    for index in range(segment_count):
        start = vertices[index]
        end_index = (index + 1) % len(vertices)
        end = vertices[end_index]
        segment_length = hypot(end[0] - start[0], end[1] - start[1])
        if segment_length <= 1e-9:
            continue

        if closed:
            before = vertices[(index - 1) % len(vertices)]
            after = vertices[(end_index + 1) % len(vertices)]
            start_scale = end_scale = 0.10
        else:
            before = vertices[max(0, index - 1)]
            after = vertices[min(len(vertices) - 1, end_index + 1)]
            start_scale = 0.24 if index == 0 else 0.10
            end_scale = 0.24 if end_index == len(vertices) - 1 else 0.10

        start_tangent = clamped_tangent(
            (end[0] - before[0]) * start_scale,
            (end[1] - before[1]) * start_scale,
            segment_length * 0.30,
        )
        end_tangent = clamped_tangent(
            (after[0] - start[0]) * end_scale,
            (after[1] - start[1]) * end_scale,
            segment_length * 0.30,
        )
        control_1 = (
            start[0] + start_tangent[0],
            start[1] + start_tangent[1],
        )
        control_2 = (
            end[0] - end_tangent[0],
            end[1] - end_tangent[1],
        )
        if bounds is not None:
            left, top, right, bottom = bounds
            control_1 = (
                min(max(control_1[0], left), right),
                min(max(control_1[1], top), bottom),
            )
            control_2 = (
                min(max(control_2[0], left), right),
                min(max(control_2[1], top), bottom),
            )
        chunks.append(
            "C "
            f"{format_number(control_1[0])},{format_number(control_1[1])} "
            f"{format_number(control_2[0])},{format_number(control_2[1])} "
            f"{format_number(end[0])},{format_number(end[1])}"
        )
    if closed:
        chunks.append("Z")
    return " ".join(chunks)


def path_data(
    points: list[Point],
    *,
    smooth: bool = False,
    bounds: tuple[float, float, float, float] | None = None,
) -> str:
    return smooth_path_data(points, bounds=bounds) if smooth else linear_path_data(points)


def append_vector_strokes(
    group: ET.Element,
    strokes: list[Stroke],
    attributes: dict[str, str] | None = None,
) -> int:
    """Emit one SVG path per uninterrupted pen-down stroke."""

    emitted = 0
    for stroke_index, stroke in enumerate(strokes, start=1):
        if len(stroke) < 2:
            continue
        path_attributes = dict(attributes or {})
        path_attributes["d"] = path_data(stroke)
        path_attributes["data-physical-stroke"] = str(stroke_index)
        ET.SubElement(group, svg_tag("path"), path_attributes)
        emitted += 1
    return emitted


def reliable_vector_strokes(
    strokes: list[Stroke],
    *,
    nib_mm: float,
) -> list[Stroke]:
    """Fail closed if lettering would lose a component below three nibs."""

    minimum_length_mm = 3 * nib_mm
    reliable = [
        stroke
        for stroke in strokes
        if len(stroke) >= 2 and polyline_length(stroke) + 1e-9 >= minimum_length_mm
    ]
    if len(reliable) != len(strokes):
        raise MapPlotterError(
            "Plotter lettering contains a component below the binding "
            f"three-nib minimum of {minimum_length_mm:g} mm. Redesign the glyph "
            "or increase its cap height; copy may not be silently pruned."
        )
    return reliable


def layer_stats(
    *,
    layer_id: str,
    label: str,
    pen: str,
    ink: str,
    nib_mm: float,
    strokes: int,
    passes: int,
    color: str,
    plotted_width_mm: float,
    path_count: int,
    length_mm: float,
    emitted: bool,
    svg_group_id: str | None,
    svg_layer_label: str | None,
    stroke_count_options: list[int] | None = None,
    plotted_width_options_mm: list[float] | None = None,
    nominal_nib_mm: float | None = None,
    requested_width_mm: float | None = None,
    width_fit_error_mm: float | None = None,
    offset_pitch_mm: float | None = None,
    width_fit_mode: str | None = None,
    pen_profile: str | None = None,
    logical_layer_id: str | None = None,
    pen_id: str | None = None,
    calibration_state: str | None = None,
    calibration_substrate: str | None = None,
    width_fit_error_options_mm: list[float] | None = None,
    offset_pitch_options_mm: list[float] | None = None,
) -> dict[str, Any]:
    stats = {
        "id": layer_id,
        "label": label,
        "pen": pen,
        "ink": ink,
        "nib_mm": round(nib_mm, 6),
        "strokes": strokes,
        "passes": passes,
        "plotted_width_mm": round(plotted_width_mm, 6),
        "preview_color": color,
        "preview_stroke_width_mm": round(nib_mm, 6),
        "path_count": path_count,
        "pen_down_distance_mm": round(length_mm, 1),
        "emitted": emitted,
        "svg_group_id": svg_group_id,
        "svg_layer_label": svg_layer_label,
    }
    stats["nominal_nib_mm"] = round(
        nib_mm if nominal_nib_mm is None else nominal_nib_mm, 6
    )
    resolved_requested_width_mm = (
        plotted_width_mm if requested_width_mm is None else requested_width_mm
    )
    stats["requested_width_mm"] = round(resolved_requested_width_mm, 6)
    stats["width_fit_error_mm"] = round(
        plotted_width_mm - resolved_requested_width_mm
        if width_fit_error_mm is None
        else width_fit_error_mm,
        6,
    )
    stats["offset_pitch_mm"] = round(
        0.0 if offset_pitch_mm is None else offset_pitch_mm,
        6,
    )
    stats["width_fit_mode"] = width_fit_mode or "style-defined"
    stats["pen_profile"] = pen_profile or "style"
    if pen_id is not None:
        stats["pen_id"] = pen_id
    stats["calibration_state"] = calibration_state or "nominal-unmeasured"
    stats["calibration_substrate"] = calibration_substrate
    if logical_layer_id is not None:
        stats["logical_layer_id"] = logical_layer_id
    if stroke_count_options is not None:
        stats["stroke_count_options"] = stroke_count_options
    if plotted_width_options_mm is not None:
        stats["plotted_width_options_mm"] = [
            round(width, 6) for width in plotted_width_options_mm
        ]
    if width_fit_error_options_mm is not None:
        stats["width_fit_error_options_mm"] = [
            round(error, 6) for error in width_fit_error_options_mm
        ]
    if offset_pitch_options_mm is not None:
        stats["offset_pitch_options_mm"] = [
            round(pitch, 6) for pitch in offset_pitch_options_mm
        ]
    if not emitted:
        stats["omission_reason"] = "no compiled strokes"
    return stats


def physical_group_attributes(
    *,
    ink: str,
    nib_mm: float,
    strokes: int,
    passes: int,
    plotted_width_mm: float,
    nominal_nib_mm: float | None = None,
    requested_width_mm: float | None = None,
    width_fit_error_mm: float | None = None,
    offset_pitch_mm: float | None = None,
    width_fit_mode: str | None = None,
    pen_profile: str | None = None,
    pen_id: str | None = None,
    calibration_state: str | None = None,
    calibration_substrate: str | None = None,
) -> dict[str, str]:
    """Machine-readable plot assumptions shared by every SVG layer group."""

    attributes = {
        "data-plot-ink": ink,
        "data-plot-nib-mm": format_measurement(nib_mm),
        "data-plot-nominal-nib-mm": format_measurement(
            nib_mm if nominal_nib_mm is None else nominal_nib_mm
        ),
        "data-plot-strokes": str(strokes),
        "data-plot-passes": str(passes),
        "data-plot-width-mm": format_measurement(plotted_width_mm),
        "data-plot-requested-width-mm": format_measurement(
            plotted_width_mm if requested_width_mm is None else requested_width_mm
        ),
        "data-plot-width-fit-error-mm": format_measurement(
            plotted_width_mm
            - (plotted_width_mm if requested_width_mm is None else requested_width_mm)
            if width_fit_error_mm is None
            else width_fit_error_mm
        ),
        "data-plot-offset-pitch-mm": format_measurement(
            0.0 if offset_pitch_mm is None else offset_pitch_mm
        ),
        "data-plot-width-fit-mode": width_fit_mode or "style-defined",
    }
    if pen_profile is not None:
        attributes["data-plot-pen-profile"] = pen_profile
    if pen_id is not None:
        attributes["data-plot-pen-id"] = pen_id
    if calibration_state is not None:
        attributes["data-plot-calibration-state"] = calibration_state
    if calibration_substrate is not None:
        attributes["data-plot-calibration-substrate"] = calibration_substrate
    return attributes


def decoration_pen_plan(
    *,
    ink: str,
    requested_width_mm: float,
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> PenWidthFit:
    if pen_inventory is None:
        return style_pen_width(
            ink=ink,
            nib_mm=requested_width_mm,
            stroke_count=1,
        )
    plan = fit_pen_width(
        pen_inventory,
        ink=ink,
        requested_width_mm=requested_width_mm,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    if plan.stroke_count != 1:
        raise MapPlotterError(
            f"Decoration target {requested_width_mm:g} mm needs parallel offsets "
            f"with pen profile {pen_inventory.id!r}, but this decoration has no "
            "safe offset construction. Choose a broader compatible pen."
        )
    return plan
