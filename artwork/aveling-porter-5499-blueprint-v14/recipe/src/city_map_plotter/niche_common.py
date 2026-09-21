"""Shared, pen-physical engine for map and non-map plate subjects.

The city renderer owns geographic acquisition and cartographic cleanup. This
module owns the common plate contract used by focused subjects such as hiking
routes and architecture: binding paper zones, the real pen inventory, editable
stroke-only SVG, a plot manifest, and deterministic geometry helpers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET

from . import __version__
from .geometry import load_plate_format, polyline_length
from .models import MapPlotterError
from .pens import (
    ACTUAL_PEN_INVENTORY,
    BUILTIN_PEN_INVENTORIES,
    PenInventory,
    PhysicalPen,
)
from .stroke_font import stroke_text, text_width_mm
from .svgkit import (
    INKSCAPE_NS,
    MAP_NS,
    SODIPODI_NS,
    SVG_COORDINATE_QUANTUM_MM,
    format_number,
    format_measurement,
    path_data,
    physical_group_attributes,
    reliable_vector_strokes,
    svg_tag,
)
from .vector_path import CubicSegment, LineSegment, VectorPath


Point = tuple[float, float]
Stroke = list[Point]


PEN_ORDER = (
    "grey-0-25",
    "grey-0-4",
    "green-0-25",
    "green-0-4",
    "blue-0-25",
    "blue-0-4",
    "red-0-25",
    "red-0-4",
    "purple-0-25",
    "purple-0-4",
    "black-0-25",
    "black-0-4",
    "black-0-6",
    "black-1",
    "white-0-3",
    "white-0-4",
    "white-0-5",
    "white-0-7",
    "white-1",
    "gold-1",
    "silver-1",
)

PENS_BY_ID: dict[str, PhysicalPen] = {
    pen.identity: pen
    for inventory in BUILTIN_PEN_INVENTORIES.values()
    for pen in inventory.pens
}
CIRCUIT_DOMAINS = frozenset({"f1-circuits", "motorsport-circuits"})


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def left(self) -> float:
        return self.x

    @property
    def top(self) -> float:
        return self.y

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def centre(self) -> Point:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def inset(self, amount: float) -> "Rect":
        if amount < 0 or 2 * amount >= min(self.width, self.height):
            raise MapPlotterError(f"Invalid {amount:g} mm inset for {self!r}.")
        return Rect(
            self.x + amount,
            self.y + amount,
            self.width - 2 * amount,
            self.height - 2 * amount,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class PlateContext:
    format_id: str
    plate: dict[str, Any]
    page: Rect
    safe: Rect
    field: Rect
    zones: dict[str, Rect]

    @classmethod
    def load(cls, format_id: str) -> "PlateContext":
        plate = load_plate_format(format_id)
        page_raw = plate["page_mm"]
        width = float(page_raw["width"])
        height = float(page_raw["height"])
        safe_margin = float(plate["safe_margin_mm"])
        zones = {
            name: Rect(
                float(record["x"]),
                float(record["y"]),
                float(record["width"]),
                float(record["height"]),
            )
            for name, record in plate["zones_mm"].items()
        }
        return cls(
            format_id=format_id,
            plate=plate,
            page=Rect(0.0, 0.0, width, height),
            safe=Rect(
                safe_margin,
                safe_margin,
                width - 2 * safe_margin,
                height - 2 * safe_margin,
            ),
            field=zones["map_field"],
            zones=zones,
        )


@dataclass(frozen=True)
class NormalizedCanvas:
    """Map 0..100 authoring coordinates into a physical plate rectangle."""

    rect: Rect

    def point(self, x: float, y: float) -> Point:
        return (
            self.rect.x + self.rect.width * float(x) / 100.0,
            self.rect.y + self.rect.height * float(y) / 100.0,
        )

    def points(self, values: Iterable[Sequence[float]]) -> Stroke:
        return [self.point(float(value[0]), float(value[1])) for value in values]

    def dx(self, value: float) -> float:
        return self.rect.width * value / 100.0

    def dy(self, value: float) -> float:
        return self.rect.height * value / 100.0


@dataclass
class StrokeRecord:
    points: Stroke
    vector_path: VectorPath | None = None
    source_ref: str | None = None
    role: str | None = None
    sequence: int | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class ArtworkLayer:
    id: str
    label: str
    pen_id: str
    records: list[StrokeRecord] = field(default_factory=list)

    @property
    def pen(self) -> PhysicalPen:
        try:
            return PENS_BY_ID[self.pen_id]
        except KeyError as exc:
            raise MapPlotterError(
                f"Layer {self.id!r} asks for unknown studio pen {self.pen_id!r}."
            ) from exc

    def add(
        self,
        points: Iterable[Sequence[float]],
        *,
        source_ref: str | None = None,
        role: str | None = None,
        sequence: int | None = None,
        attributes: dict[str, str] | None = None,
    ) -> None:
        stroke = [(float(point[0]), float(point[1])) for point in points]
        if len(stroke) < 2:
            raise MapPlotterError(f"Layer {self.id!r} received a one-point stroke.")
        self.records.append(
            StrokeRecord(
                points=stroke,
                source_ref=source_ref,
                role=role,
                sequence=sequence,
                attributes=dict(attributes or {}),
            )
        )

    def add_many(
        self,
        strokes: Iterable[Iterable[Sequence[float]]],
        **metadata: Any,
    ) -> None:
        for stroke in strokes:
            self.add(stroke, **metadata)

    def add_path(
        self,
        path: VectorPath,
        *,
        source_ref: str | None = None,
        role: str | None = None,
        sequence: int | None = None,
        attributes: dict[str, str] | None = None,
        flatten_tolerance_mm: float = 0.01,
    ) -> None:
        """Add exact line/cubic geometry while retaining a planning polyline.

        The flattened points exist only for endpoint/travel compatibility with
        older plate domains. SVG emission, length, and bounds use ``path``
        directly, so imported source curves are never replaced by a smoothed or
        resampled drawing.
        """

        flattened = path.flatten(flatten_tolerance_mm)
        points = [(float(x), float(y)) for x, y in flattened.points]
        if len(points) < 2:
            raise MapPlotterError(f"Layer {self.id!r} received an empty vector path.")
        self.records.append(
            StrokeRecord(
                points=points,
                vector_path=path,
                source_ref=source_ref,
                role=role,
                sequence=sequence,
                attributes=dict(attributes or {}),
            )
        )


@dataclass
class PlateArtwork:
    subject_id: str
    domain: str
    subject_kind: str
    title: str
    subtitle: str
    details: tuple[str, ...]
    credit_line: str
    scale_status: str
    evidence_status: str
    rights_status: str
    sources: tuple[dict[str, Any], ...]
    context: PlateContext
    layers: list[ArtworkLayer]
    variant_id: str | None = None
    pen_order: tuple[str, ...] = ()
    artifact_kind: str = "hiking-pen-map"
    rendering_preset: str = "hiking-map-a5-v2"
    format_subject_policy: str | None = None
    source_provider: str = "curated multi-source catalog"
    source_license: str = "per-source; see sources"
    data_snapshot: str = "2026-08-02"
    notes: tuple[str, ...] = ()
    catalog_record: dict[str, Any] = field(default_factory=dict)
    rendering_metadata: dict[str, Any] = field(default_factory=dict)
    # Some subjects carry a long canonical title in metadata while the binding
    # title zone uses a supplied compact plate heading.  Existing domains leave
    # this unset, preserving their established SVG and manifest output.
    document_title: str | None = None
    rights_metadata: dict[str, Any] = field(default_factory=dict)
    # Optional domain-specific evidence repeated in the SVG metadata JSON.  It
    # defaults to empty so existing byte-level render contracts are unchanged.
    svg_metadata: dict[str, Any] = field(default_factory=dict)
    # A domain may select one of the border styles already allowed by the
    # binding plate.  This changes only whether the standard furniture emits
    # its border; it never introduces a new page constant.
    border_style: str | None = None
    # Four-card fact composition used by circuit studies. Each record carries
    # a short label and one or two value lines; the generated format contract,
    # not the subject renderer, owns their paper rectangles.
    information_groups: tuple[dict[str, Any], ...] = ()
    # Specialist plates may supply their own format-derived furniture layers.
    # The default remains the shared compositor for every existing caller.
    include_standard_furniture: bool = True
    # Preview paper is not a plotted fill.  It controls the Inkscape page and
    # PNG export only; all motor paths remain inside physical pen layers.
    preview_background: str = "#ffffff"
    stock_tone: str = "light"
    visible_attribution: bool = True
    # A specialist plate may bind a separate real pen set without changing the
    # default studio profile embedded in unrelated existing artwork.
    pen_inventory: PenInventory = ACTUAL_PEN_INVENTORY

    @property
    def artifact_id(self) -> str:
        """Return the file-safe identity of this rendered subject variant."""

        if self.variant_id is None:
            return self.subject_id
        variant_id = self.variant_id.strip()
        if not variant_id or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in variant_id
        ):
            raise MapPlotterError(
                "Plate variant IDs must use lower-case ASCII letters, digits, and hyphens."
            )
        return f"{self.subject_id}--{variant_id}"

    def layer(self, layer_id: str, label: str, pen_id: str) -> ArtworkLayer:
        for layer in self.layers:
            if layer.id == layer_id:
                if layer.pen_id != pen_id:
                    raise MapPlotterError(
                        f"Logical layer {layer_id!r} changed pen from "
                        f"{layer.pen_id!r} to {pen_id!r}."
                    )
                return layer
        layer = ArtworkLayer(layer_id, label, pen_id)
        self.layers.append(layer)
        return layer


def context_for(format_id: str = "a5-portrait") -> PlateContext:
    return PlateContext.load(format_id)


def field_canvas(context: PlateContext, padding_mm: float = 4.0) -> NormalizedCanvas:
    return NormalizedCanvas(context.field.inset(padding_mm))


def polyline_length_mm(points: Sequence[Point]) -> float:
    return polyline_length(list(points))


def stroke_record_length_mm(record: StrokeRecord) -> float:
    """Return the physical path length without degrading exact cubic geometry."""

    if record.vector_path is not None:
        return record.vector_path.length(0.001)
    return polyline_length_mm(record.points)


def _stroke_record_bounds(record: StrokeRecord) -> tuple[float, float, float, float]:
    if record.vector_path is not None:
        bounds = record.vector_path.bounds()
        return (bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)
    xs = [point[0] for point in record.points]
    ys = [point[1] for point in record.points]
    return (min(xs), min(ys), max(xs), max(ys))


def rectangle_stroke(rect: Rect) -> Stroke:
    return [
        (rect.left, rect.top),
        (rect.right, rect.top),
        (rect.right, rect.bottom),
        (rect.left, rect.bottom),
        (rect.left, rect.top),
    ]


def circle_stroke(centre: Point, radius_mm: float, *, segments: int = 32) -> Stroke:
    if radius_mm <= 0 or segments < 8:
        raise MapPlotterError("A plotted circle needs a positive radius and 8+ sides.")
    cx, cy = centre
    points = [
        (
            cx + radius_mm * math.cos(2 * math.pi * index / segments),
            cy + radius_mm * math.sin(2 * math.pi * index / segments),
        )
        for index in range(segments)
    ]
    return [*points, points[0]]


def ellipse_stroke(
    centre: Point,
    radius_x_mm: float,
    radius_y_mm: float,
    *,
    segments: int = 64,
    rotation_deg: float = 0.0,
) -> Stroke:
    if min(radius_x_mm, radius_y_mm) <= 0 or segments < 12:
        raise MapPlotterError("A plotted ellipse needs positive radii and 12+ sides.")
    angle = math.radians(rotation_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    cx, cy = centre
    points: Stroke = []
    for index in range(segments):
        phase = 2 * math.pi * index / segments
        local_x = radius_x_mm * math.cos(phase)
        local_y = radius_y_mm * math.sin(phase)
        points.append(
            (
                cx + local_x * cosine - local_y * sine,
                cy + local_x * sine + local_y * cosine,
            )
        )
    return [*points, points[0]]


def arc_stroke(
    centre: Point,
    radius_x_mm: float,
    radius_y_mm: float,
    start_deg: float,
    end_deg: float,
    *,
    segments: int = 24,
) -> Stroke:
    if segments < 2:
        raise MapPlotterError("An arc needs at least two segments.")
    cx, cy = centre
    return [
        (
            cx + radius_x_mm * math.cos(math.radians(angle)),
            cy + radius_y_mm * math.sin(math.radians(angle)),
        )
        for angle in (
            start_deg + (end_deg - start_deg) * index / segments
            for index in range(segments + 1)
        )
    ]


def arrow_strokes(points: Sequence[Point], *, head_mm: float = 2.4) -> list[Stroke]:
    if len(points) < 2:
        raise MapPlotterError("An arrow needs a start and an end.")
    end = points[-1]
    previous = points[-2]
    dx = end[0] - previous[0]
    dy = end[1] - previous[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        raise MapPlotterError("An arrow cannot end with a zero-length segment.")
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    spread = head_mm * 0.55
    left = (end[0] - ux * head_mm + px * spread, end[1] - uy * head_mm + py * spread)
    right = (end[0] - ux * head_mm - px * spread, end[1] - uy * head_mm - py * spread)
    return [list(points), [left, end, right]]


def cross_strokes(centre: Point, radius_mm: float) -> list[Stroke]:
    x, y = centre
    return [
        [(x - radius_mm, y), (x + radius_mm, y)],
        [(x, y - radius_mm), (x, y + radius_mm)],
    ]


def chaikin(points: Sequence[Point], *, iterations: int = 2) -> Stroke:
    """Corner-cut an open route while retaining both source endpoints."""

    result = list(points)
    for _ in range(max(iterations, 0)):
        if len(result) < 3:
            break
        refined: Stroke = [result[0]]
        for first, second in zip(result, result[1:]):
            refined.extend(
                [
                    (
                        first[0] * 0.75 + second[0] * 0.25,
                        first[1] * 0.75 + second[1] * 0.25,
                    ),
                    (
                        first[0] * 0.25 + second[0] * 0.75,
                        first[1] * 0.25 + second[1] * 0.75,
                    ),
                ]
            )
        refined.append(result[-1])
        result = refined
    return result


def normalize_points(
    points: Sequence[Point],
    rect: Rect,
    *,
    preserve_aspect: bool = True,
    invert_y: bool = True,
) -> Stroke:
    """Fit arbitrary Cartesian coordinates into a physical rectangle."""

    if len(points) < 2:
        raise MapPlotterError("At least two points are required for normalization.")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-12)
    span_y = max(max_y - min_y, 1e-12)
    scale_x = rect.width / span_x
    scale_y = rect.height / span_y
    if preserve_aspect:
        scale_x = scale_y = min(scale_x, scale_y)
    used_width = span_x * scale_x
    used_height = span_y * scale_y
    offset_x = rect.x + (rect.width - used_width) / 2
    offset_y = rect.y + (rect.height - used_height) / 2
    result: Stroke = []
    for x, y in points:
        relative_y = max_y - y if invert_y else y - min_y
        result.append(
            (
                offset_x + (x - min_x) * scale_x,
                offset_y + relative_y * scale_y,
            )
        )
    return result


def text_strokes_fit(
    text: str,
    *,
    x_mm: float,
    y_mm: float,
    preferred_cap_mm: float,
    maximum_width_mm: float,
    pen_id: str,
    anchor: str = "start",
    minimum_cap_mm: float | None = None,
    allow_horizontal_condense: bool = False,
) -> list[Stroke]:
    text = plotter_copy(text)
    pen = PENS_BY_ID[pen_id]
    physical_minimum = 8.0 * pen.mark_width_mm
    floor = max(physical_minimum, minimum_cap_mm or 0.0)
    natural_width = text_width_mm(text, cap_height_mm=preferred_cap_mm)
    if natural_width <= 0:
        raise MapPlotterError("A text block must contain drawable characters.")
    cap_height = min(
        preferred_cap_mm,
        preferred_cap_mm * maximum_width_mm / natural_width,
    )
    if cap_height + 1e-9 < floor and allow_horizontal_condense:
        strokes = stroke_text(
            text,
            x_mm=x_mm,
            y_mm=y_mm,
            height_mm=max(preferred_cap_mm, floor),
            anchor=anchor,
        )
        x_values = [point[0] for stroke in strokes for point in stroke]
        width = max(x_values) - min(x_values) if x_values else 0.0
        usable_width = max(maximum_width_mm - 2.0 * pen.mark_width_mm, 0.0)
        width_scale = min(1.0, usable_width / width) if width > 0.0 else 1.0
        if width_scale >= 0.65:
            condensed = [
                [(x_mm + (point[0] - x_mm) * width_scale, point[1]) for point in stroke]
                for stroke in strokes
            ]
            minimum_length = 3.0 * pen.mark_width_mm
            # Paths are serialized to the neutral SVG coordinate quantum.
            # Reinforce past the exact floor so endpoint rounding cannot turn
            # an internally valid glyph component into a sub-three-nib dot.
            reinforced_minimum = minimum_length + 2.0 * SVG_COORDINATE_QUANTUM_MM
            reinforced: list[Stroke] = []
            for stroke in condensed:
                if polyline_length(stroke) + 1e-9 >= reinforced_minimum:
                    reinforced.append(stroke)
                    continue
                minimum_x = min(point[0] for point in stroke)
                maximum_x = max(point[0] for point in stroke)
                if maximum_x - minimum_x <= 1e-9:
                    reinforced.append(stroke)
                    continue
                centre_x = (minimum_x + maximum_x) / 2.0
                low, high = 1.0, 2.0
                candidate = stroke
                for _ in range(24):
                    factor = (low + high) / 2.0
                    candidate = [
                        (centre_x + (point[0] - centre_x) * factor, point[1])
                        for point in stroke
                    ]
                    if polyline_length(candidate) < reinforced_minimum:
                        low = factor
                    else:
                        high = factor
                candidate = [
                    (
                        centre_x + (point[0] - centre_x) * high * 1.000001,
                        point[1],
                    )
                    for point in stroke
                ]
                reinforced.append(candidate)
            reinforced_x = [point[0] for stroke in reinforced for point in stroke]
            if (
                reinforced_x
                and max(reinforced_x) - min(reinforced_x) > maximum_width_mm + 1e-9
            ):
                raise MapPlotterError(
                    "Condensed attribution glyph reinforcement exceeds its zone."
                )
            return reliable_vector_strokes(reinforced, nib_mm=pen.mark_width_mm)
    if cap_height + 1e-9 < floor:
        raise MapPlotterError(
            f"Text {text!r} needs {cap_height:.3f} mm cap height to fit, below "
            f"the {floor:g} mm physical floor for {pen.label}."
        )
    strokes = stroke_text(
        text,
        x_mm=x_mm,
        y_mm=y_mm,
        height_mm=cap_height,
        anchor=anchor,
    )
    return reliable_vector_strokes(strokes, nib_mm=pen.mark_width_mm)


def plotter_copy(text: str) -> str:
    """Translate common editorial Unicode into supported stroke-font copy."""

    replacements = str.maketrans(
        {
            "·": "/",
            "•": "/",
            "–": "-",
            "—": "-",
            "−": "-",
            "’": "'",
            "‘": "'",
            "“": '"',
            "”": '"',
            "×": "X",
            "→": ">",
            "←": "<",
            "…": "...",
        }
    )
    return " ".join(str(text).translate(replacements).split())


def add_text(
    layer: ArtworkLayer,
    text: str,
    *,
    x_mm: float,
    y_mm: float,
    preferred_cap_mm: float,
    maximum_width_mm: float,
    anchor: str = "start",
    minimum_cap_mm: float | None = None,
    allow_horizontal_condense: bool = False,
    source_ref: str | None = None,
    role: str = "label",
    attributes: dict[str, str] | None = None,
) -> None:
    layer.add_many(
        text_strokes_fit(
            text,
            x_mm=x_mm,
            y_mm=y_mm,
            preferred_cap_mm=preferred_cap_mm,
            maximum_width_mm=maximum_width_mm,
            pen_id=layer.pen_id,
            anchor=anchor,
            minimum_cap_mm=minimum_cap_mm,
            allow_horizontal_condense=allow_horizontal_condense,
        ),
        source_ref=source_ref,
        role=role,
        attributes=attributes,
    )


def add_number_marker(
    outline: ArtworkLayer,
    labels: ArtworkLayer,
    centre: Point,
    value: str,
    *,
    radius_mm: float = 1.9,
    source_ref: str | None = None,
) -> None:
    # Marker copy must obey the same 8 x nib floor as every other label.  The
    # historic 2.1 mm default is valid for a 0.25 mm label pen but not for the
    # technical plate's 0.40 mm label role, so resolve the complete marker from
    # the actual copy pen rather than silently shrinking its numeral.
    label_cap_mm = max(2.1, 8.0 * labels.pen.mark_width_mm)
    label_width_mm = max(2.7, 1.2 * label_cap_mm)
    resolved_radius_mm = max(radius_mm, 0.9 * label_cap_mm)
    outline.add(
        circle_stroke(centre, resolved_radius_mm, segments=20),
        source_ref=source_ref,
        role="marker",
    )
    add_text(
        labels,
        value,
        x_mm=centre[0],
        y_mm=centre[1] - 0.5 * label_cap_mm,
        preferred_cap_mm=label_cap_mm,
        maximum_width_mm=label_width_mm,
        anchor="middle",
        source_ref=source_ref,
        role="marker-label",
    )


def _add_title(
    layer: ArtworkLayer,
    title: str,
    zone: Rect,
    plate: dict[str, Any],
    *,
    allow_horizontal_condense: bool = False,
) -> None:
    preferred = float(plate["type_scale_mm"]["title"])
    minimum = float(plate["rules"]["min_cap_height_mm"]["title"])
    layout_rule = plate["rules"]["title_line_layout"]
    contract_nib = float(layout_rule["nib_mm"])
    if not math.isclose(
        layer.pen.mark_width_mm,
        contract_nib,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise MapPlotterError(
            "The title compositor pen does not match the binding format "
            f"contract ({layer.pen.mark_width_mm:g} mm != {contract_nib:g} mm)."
        )
    minimum_ink_clearance = float(layout_rule["min_ink_clearance_mm"])
    line_bounds_gap = float(layout_rule["min_path_bounds_gap_mm"])
    horizontal_ink_inset = float(layout_rule["horizontal_ink_inset_mm"])
    if horizontal_ink_inset + 1e-9 < layer.pen.mark_width_mm / 2.0:
        raise MapPlotterError(
            "The binding title inset does not contain the physical title nib."
        )
    maximum_path_width = zone.width - 2.0 * horizontal_ink_inset
    if maximum_path_width <= 0.0:
        raise MapPlotterError("The binding title zone has no physical text width.")
    required_bounds_gap = layer.pen.mark_width_mm + minimum_ink_clearance
    if line_bounds_gap + 1e-9 < required_bounds_gap:
        raise MapPlotterError(
            "The binding title line gap does not clear the physical title nib."
        )
    copy_text = plotter_copy(title)
    natural_width = text_width_mm(copy_text, cap_height_mm=preferred)
    fitted_single_cap = min(
        preferred,
        preferred * maximum_path_width / natural_width,
    )
    # Preserve the established one-line selection gate (the natural title had
    # to fit the zone), but fit that accepted title inside the physical nib
    # inset. This keeps borderline one-word titles such as SPIELBERG on one
    # line without letting their round-capped ink escape the title zone.
    if (
        natural_width <= zone.width + 1e-9
        and fitted_single_cap + 1e-9 >= minimum
    ):
        add_text(
            layer,
            copy_text,
            x_mm=zone.centre[0],
            y_mm=zone.y + (zone.height - fitted_single_cap) / 2,
            preferred_cap_mm=preferred,
            maximum_width_mm=maximum_path_width,
            anchor="middle",
            minimum_cap_mm=minimum,
            role="title",
            attributes={
                "data-title-block-id": "plate-title",
                "data-title-line-index": "0",
                "data-title-line-count": "1",
            },
        )
        return

    words = copy_text.split()
    if len(words) == 1 and allow_horizontal_condense:
        # Some source-faithful proper names have no legitimate line-break
        # opportunity.  Keep the cap height and physical nib envelope intact,
        # using the same bounded (>=65%) horizontal condensation already
        # permitted for split title lines.  ``text_strokes_fit`` remains the
        # authoritative feasibility gate and raises if the word is too wide.
        add_text(
            layer,
            copy_text,
            x_mm=zone.centre[0],
            y_mm=zone.y + (zone.height - preferred) / 2,
            preferred_cap_mm=preferred,
            maximum_width_mm=maximum_path_width,
            anchor="middle",
            minimum_cap_mm=minimum,
            allow_horizontal_condense=True,
            role="title",
            attributes={
                "data-title-block-id": "plate-title",
                "data-title-line-index": "0",
                "data-title-line-count": "1",
            },
        )
        return
    candidates: list[tuple[float, float, str, str]] = []
    for split in range(1, len(words)):
        first = " ".join(words[:split])
        second = " ".join(words[split:])
        widest = max(
            text_width_mm(first, cap_height_mm=minimum),
            text_width_mm(second, cap_height_mm=minimum),
        )
        if widest <= maximum_path_width + 1e-9:
            imbalance = abs(len(first) - len(second))
            candidates.append((widest, imbalance, first, second))
    if not candidates and not allow_horizontal_condense:
        raise MapPlotterError(
            f"Title {title!r} cannot fit the binding title zone at the "
            f"{minimum:g} mm physical floor."
        )
    if candidates:
        _, _, first, second = min(candidates, key=lambda value: (value[0], value[1]))
    else:
        split_candidates = [
            (
                max(
                    text_width_mm(" ".join(words[:split]), cap_height_mm=minimum),
                    text_width_mm(" ".join(words[split:]), cap_height_mm=minimum),
                ),
                abs(len(" ".join(words[:split])) - len(" ".join(words[split:]))),
                " ".join(words[:split]),
                " ".join(words[split:]),
            )
            for split in range(1, len(words))
        ]
        if not split_candidates:
            raise MapPlotterError(
                f"Title {title!r} cannot split inside the binding title zone."
            )
        _, _, first, second = min(
            split_candidates, key=lambda value: (value[0], value[1])
        )
    maximum_lines = int(layout_rule["maximum_lines"])
    if maximum_lines < 2:
        raise MapPlotterError(
            "The binding title layout does not permit a two-line title."
        )
    line_gap = line_bounds_gap
    block_height = 2 * minimum + line_gap
    if block_height > zone.height + 1e-9:
        raise MapPlotterError(
            f"Title {title!r} needs {block_height:g} mm of title-zone height "
            f"under the physical nib-clearance contract; only {zone.height:g} mm "
            "is available."
        )
    y = zone.y + (zone.height - block_height) / 2
    for index, line in enumerate((first, second)):
        add_text(
            layer,
            line,
            x_mm=zone.centre[0],
            y_mm=y + index * (minimum + line_gap),
            preferred_cap_mm=minimum,
            maximum_width_mm=maximum_path_width,
            anchor="middle",
            minimum_cap_mm=minimum,
            allow_horizontal_condense=allow_horizontal_condense,
            role="title",
            attributes={
                "data-title-block-id": "plate-title",
                "data-title-line-index": str(index),
                "data-title-line-count": "2",
            },
        )


def _pen_id_for_ink_and_nib(ink: str, nib_mm: float) -> str:
    matches = [
        pen.identity
        for pen in ACTUAL_PEN_INVENTORY.pens
        if pen.ink.casefold() == ink.casefold()
        and math.isclose(pen.mark_width_mm, nib_mm, abs_tol=1e-9)
    ]
    if len(matches) != 1:
        raise MapPlotterError(
            f"The studio inventory has no unique {ink} {nib_mm:g} mm pen."
        )
    return matches[0]


def _furniture_pen_id(plate: dict[str, Any], role: str) -> str:
    try:
        nib_role = str(plate["type_nib_role"][role])
        nib_mm = float(plate["nib_roles_mm"][nib_role])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Plate format does not resolve the {role!r} furniture nib."
        ) from exc
    return _pen_id_for_ink_and_nib("Black", nib_mm)


def bridge_pen_id(plate: dict[str, Any], role: str) -> str:
    """Resolve one bridge semantic role from the generated format contract."""

    try:
        record = plate["bridge_pen_roles"][role]
        ink = str(record["ink"])
        nib_mm = float(record["nib_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Plate format does not resolve the {role!r} bridge pen role."
        ) from exc
    return _pen_id_for_ink_and_nib(ink, nib_mm)


def technical_pen_id(plate: dict[str, Any], semantic_class: str) -> str:
    """Resolve an engineered-object semantic class to one real studio pen."""

    try:
        record = plate["technical_pen_roles"][semantic_class]
        ink = str(record["ink"])
        nib_mm = float(record["nib_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            "Plate format does not resolve technical semantic class "
            f"{semantic_class!r}."
        ) from exc
    return _pen_id_for_ink_and_nib(ink, nib_mm)


def _bridge_furniture_layers(artwork: PlateArtwork) -> list[ArtworkLayer]:
    """Assemble bridge furniture whose geometry is owned by ``furniture.py``."""

    from .furniture import bridge_furniture_plan

    context = artwork.context
    plate = context.plate
    heavy = ArtworkLayer(
        "plate_heavy",
        "Border and title",
        _furniture_pen_id(plate, "title"),
    )
    copy_layer = ArtworkLayer(
        "plate_copy",
        "Subtitle and details",
        _furniture_pen_id(plate, "detail"),
    )
    attribution = ArtworkLayer(
        "plate_attribution",
        "Attribution",
        _furniture_pen_id(plate, "attribution"),
    )
    reference = ArtworkLayer(
        "field_frame",
        "Field frame",
        bridge_pen_id(plate, "frame"),
    )
    dimensions = artwork.catalog_record.get("model", {}).get("dimensions", [])
    if not isinstance(dimensions, list) or not dimensions:
        raise MapPlotterError("Bridge furniture requires catalog dimensions.")
    fidelity = artwork.catalog_record.get("fidelity", {})
    source_profile = (
        isinstance(fidelity, dict) and fidelity.get("status") == "source-profile"
    )
    plan = bridge_furniture_plan(
        format_id=context.format_id,
        title=artwork.title,
        subtitle=artwork.subtitle,
        details=artwork.details,
        credit_lines=[
            line.strip() for line in artwork.credit_line.split(" | ") if line.strip()
        ],
        dimension_labels=[
            str(dimension["label"])
            for dimension in dimensions
            if isinstance(dimension, dict) and dimension.get("label")
        ],
        field_label=(
            "SIDE ELEVATION / SOURCE PROFILE / TRACE CONTROLLED"
            if source_profile
            else "SIDE ELEVATION / DIMENSION SCHEMATIC PREVIEW"
        ),
    )
    heavy.add(plan.frame_strokes["outer-border"], role="outer-border")
    heavy.add(plan.frame_strokes["inner-border"], role="inner-border")
    reference.add(plan.frame_strokes["field-frame"], role="field-frame")
    targets = {
        "plate_heavy": heavy,
        "plate_copy": copy_layer,
        "plate_attribution": attribution,
    }
    for line in plan.text_lines:
        target = targets.get(line.layer_id)
        if target is None:
            continue
        target.add_many(
            line.strokes,
            role=line.role,
            attributes={
                "data-copy": line.copy,
                "data-cap-height-mm": format_measurement(line.cap_height_mm),
                "data-copy-geometry-sha256": line.geometry_sha256,
                "data-text-block-id": line.block_id,
                "data-text-line-index": str(line.line_index),
                "data-text-zone": line.zone,
                "data-typography-authority": ("furniture.bridge_furniture_plan-v1"),
            },
        )
    return [reference, copy_layer, attribution, heavy]


def _furniture_layers(artwork: PlateArtwork) -> list[ArtworkLayer]:
    if artwork.domain == "bridges":
        return _bridge_furniture_layers(artwork)
    context = artwork.context
    plate = context.plate
    heavy = ArtworkLayer(
        "plate_heavy",
        "Border and title",
        _furniture_pen_id(plate, "title"),
    )
    copy_layer = ArtworkLayer(
        "plate_copy",
        "Subtitle and details",
        _furniture_pen_id(plate, "detail"),
    )
    attribution = ArtworkLayer(
        "plate_attribution",
        "Attribution",
        _furniture_pen_id(plate, "attribution"),
    )
    frame_nib_mm = float(plate["map_linework_nib_mm"]["hairline"])
    reference = ArtworkLayer(
        "field_frame",
        "Field frame",
        _pen_id_for_ink_and_nib("Grey", frame_nib_mm),
    )

    border_style = artwork.border_style or str(plate["border"]["style"])
    allowed_border_styles = set(plate["border"]["allowed_styles"])
    if border_style not in allowed_border_styles:
        raise MapPlotterError(
            f"Border style {border_style!r} is not allowed by {context.format_id}."
        )
    if border_style != "none":
        heavy.add(rectangle_stroke(context.safe), role="outer-border")
    # Hiking and circuit-atlas plates use the page as an illustration rather
    # than a technical drawing card. A second heavy border and a hairline
    # around the map field made the geographic artwork read like a UI panel and
    # consumed valuable white space at A5. Keep those reference frames for the
    # other plate families, but give these atlases one quiet page border.
    quiet_atlas_domains = {"hikes", *CIRCUIT_DOMAINS, "abstract-3d"}
    if border_style == "double" and artwork.domain not in quiet_atlas_domains:
        border_inner_offset = float(plate["border"]["inner_offset_mm"])
        heavy.add(
            rectangle_stroke(context.safe.inset(border_inner_offset)),
            role="inner-border",
        )
    title_zone = context.zones["title"]
    _add_title(
        heavy,
        artwork.title,
        title_zone,
        plate,
        allow_horizontal_condense=artwork.domain
        in {"sports", *CIRCUIT_DOMAINS, "abstract-3d", "native-svg-transport"},
    )

    subtitle_zone = context.zones["subtitle"]
    subtitle_cap = float(plate["type_scale_mm"]["subtitle"])
    add_text(
        copy_layer,
        artwork.subtitle,
        x_mm=subtitle_zone.centre[0],
        y_mm=subtitle_zone.y + (subtitle_zone.height - subtitle_cap) / 2,
        preferred_cap_mm=subtitle_cap,
        maximum_width_mm=subtitle_zone.width,
        anchor="middle",
        allow_horizontal_condense=artwork.domain
        in {
            "sports",
            "technical-objects",
            *CIRCUIT_DOMAINS,
            "abstract-3d",
            "native-svg-transport",
        },
        role="subtitle",
    )

    detail_cap = float(plate["type_scale_mm"]["detail"])
    if artwork.domain in CIRCUIT_DOMAINS and artwork.information_groups:
        circuit_zones = plate.get("circuit_zones_mm") or {}
        zone_names = (
            "circuit_course",
            "circuit_history",
            "circuit_record",
            "circuit_drawing",
        )
        if len(artwork.information_groups) != len(zone_names):
            raise MapPlotterError(
                "Circuit furniture requires exactly four information groups."
            )
        if any(name not in circuit_zones for name in zone_names):
            raise MapPlotterError(
                "The binding format has no circuit information composition."
            )
        label_cap = max(
            float(plate["type_scale_mm"]["attribution"]),
            8.0 * copy_layer.pen.mark_width_mm,
        )
        line_gap = 2.0 * copy_layer.pen.mark_width_mm
        rule_inset = 2.0 * copy_layer.pen.mark_width_mm
        for zone_name, group in zip(
            zone_names, artwork.information_groups, strict=True
        ):
            raw = circuit_zones[zone_name]
            zone = Rect(
                float(raw["x"]),
                float(raw["y"]),
                float(raw["width"]),
                float(raw["height"]),
            )
            label = str(group.get("label") or "").strip()
            lines_value = group.get("lines")
            lines = (
                [str(value).strip() for value in lines_value]
                if isinstance(lines_value, (list, tuple))
                else []
            )
            if not label or not lines or len(lines) > 2 or any(not line for line in lines):
                raise MapPlotterError(
                    "Circuit information groups require a label and one or two "
                    "non-empty value lines."
                )
            copy_layer.add(
                [
                    (zone.x + rule_inset, zone.y),
                    (zone.right - rule_inset, zone.y),
                ],
                role="circuit-information-rule",
                attributes={"data-information-zone": zone_name},
            )
            total_height = (
                label_cap
                + len(lines) * detail_cap
                + len(lines) * line_gap
            )
            if total_height > zone.height + 1e-9:
                raise MapPlotterError(
                    f"Circuit information group {zone_name!r} cannot fit its "
                    "binding zone at the resolved physical type sizes."
                )
            y = zone.y + (zone.height - total_height) / 2.0
            text_x = zone.x + rule_inset
            maximum_width = zone.width - 2.0 * rule_inset
            add_text(
                copy_layer,
                label,
                x_mm=text_x,
                y_mm=y,
                preferred_cap_mm=label_cap,
                maximum_width_mm=maximum_width,
                allow_horizontal_condense=True,
                role="circuit-information-label",
                attributes={
                    "data-information-zone": zone_name,
                    "data-copy": label,
                },
            )
            y += label_cap + line_gap
            for line_index, line in enumerate(lines):
                add_text(
                    copy_layer,
                    line,
                    x_mm=text_x,
                    y_mm=y + line_index * (detail_cap + line_gap),
                    preferred_cap_mm=detail_cap,
                    maximum_width_mm=maximum_width,
                    allow_horizontal_condense=True,
                    role="circuit-information-value",
                    attributes={
                        "data-information-zone": zone_name,
                        "data-information-line": str(line_index),
                        "data-copy": line,
                    },
                )
    else:
        detail_zone = context.zones["detail"]
        detail_lines = list(artwork.details[:3])
        line_gap = (
            4.0 * copy_layer.pen.mark_width_mm
            if artwork.domain == "golf"
            else 0.9
        )
        total_height = (
            len(detail_lines) * detail_cap
            + max(len(detail_lines) - 1, 0) * line_gap
        )
        detail_y = detail_zone.y + max((detail_zone.height - total_height) / 2, 0.0)
        for index, detail in enumerate(detail_lines):
            add_text(
                copy_layer,
                detail,
                x_mm=detail_zone.centre[0],
                y_mm=detail_y + index * (detail_cap + line_gap),
                preferred_cap_mm=detail_cap,
                maximum_width_mm=detail_zone.width,
                anchor="middle",
                allow_horizontal_condense=artwork.domain
                in {
                    "hikes",
                    "sports",
                    "technical-objects",
                    *CIRCUIT_DOMAINS,
                    "abstract-3d",
                    "native-svg-transport",
                },
                role="detail",
            )

    attribution_zone = context.zones["attribution"]
    attribution_cap = float(plate["type_scale_mm"]["attribution"])
    # The A5 landscape rail is narrow.  A literal `` | `` delimiter lets a
    # plate stack the legal copy without shrinking below the binding eight-nib
    # type floor.  Two cap-height boxes must not touch, however: at physical
    # pen width their shared edge becomes a visibly overprinted baseline.
    # Hiking plates may therefore grow the stack a little *upward* into their
    # deliberately open illustration field while remaining inside the safe
    # border.  The delimiter itself is editorial metadata and is never plotted.
    attribution_lines = [
        line.strip() for line in artwork.credit_line.split(" | ") if line.strip()
    ]
    # Circuit scope belongs in the four factual cards. Keep the attribution as
    # exactly one quiet legal source-credit line so audit prose cannot migrate
    # back into the visible rail.
    if artwork.domain == "hikes":
        maximum_attribution_lines = 3
    elif artwork.domain in CIRCUIT_DOMAINS:
        maximum_attribution_lines = 1
    else:
        maximum_attribution_lines = 2
    if not attribution_lines or len(attribution_lines) > maximum_attribution_lines:
        raise MapPlotterError(
            "Attribution must contain between one and "
            f"{maximum_attribution_lines} non-empty lines."
        )
    # Seven millimetres overall gives two 2.002 mm cap lines approximately
    # 3 mm of clear leading.  That separation remains obvious after real
    # 0.25 mm pens spread on paper, not merely in the raster preview.
    if artwork.domain == "hikes":
        attribution_line_cap = attribution_cap
        attribution_gap = 3.00 if len(attribution_lines) > 1 else 0.0
    elif len(attribution_lines) > 1:
        # Two cap-height boxes that merely touch share strokes whenever the
        # lower edge of one glyph aligns with the upper edge of the next.  Give
        # multi-line technical credits a physical three-nib separation and
        # shrink both lines together, never below the binding eight-nib floor.
        attribution_gap = 3.0 * attribution.pen.mark_width_mm
        attribution_floor = max(
            float(plate["rules"]["min_cap_height_mm"]["attribution"]),
            8.0 * attribution.pen.mark_width_mm,
        )
        attribution_line_cap = min(
            attribution_cap,
            (attribution_zone.height - attribution_gap) / 2.0,
        )
        if attribution_line_cap + 1e-9 < attribution_floor:
            raise MapPlotterError(
                "Two-line attribution cannot fit its binding zone with a "
                "physical three-nib line separation."
            )
    else:
        attribution_line_cap = attribution_cap
        attribution_gap = 0.0
    attribution_step = attribution_line_cap + attribution_gap
    attribution_height = (
        len(attribution_lines) * attribution_line_cap
        + max(len(attribution_lines) - 1, 0) * attribution_gap
    )
    if artwork.domain == "hikes" and len(attribution_lines) > 1:
        attribution_y = context.safe.bottom - 2.0 - attribution_height
    else:
        attribution_y = attribution_zone.y + max(
            (attribution_zone.height - attribution_height) / 2.0,
            0.0,
        )
    for line_index, line in enumerate(attribution_lines):
        add_text(
            attribution,
            line,
            x_mm=attribution_zone.centre[0],
            y_mm=attribution_y + line_index * attribution_step,
            preferred_cap_mm=attribution_line_cap,
            maximum_width_mm=attribution_zone.width,
            anchor="middle",
            allow_horizontal_condense=artwork.domain
            in {
                "hikes",
                "sports",
                "technical-objects",
                *CIRCUIT_DOMAINS,
                "native-svg-transport",
            },
            role="attribution",
            attributes={
                "data-copy": line,
                "data-attribution-layout": (
                    "hike-condensed-separated-v2"
                    if artwork.domain == "hikes"
                    else (
                        "standard-separated-v2"
                        if len(attribution_lines) > 1
                        else "standard-v1"
                    )
                ),
            },
        )
    if artwork.domain not in quiet_atlas_domains:
        reference.add(rectangle_stroke(context.field), role="field-frame")
    return [reference, copy_layer, attribution, heavy]


def _validate_artwork(artwork: PlateArtwork, layers: Sequence[ArtworkLayer]) -> None:
    if artwork.context.plate["sheet"] not in {"A5", "A4", "A3"}:
        raise MapPlotterError("Pen plates require a binding A-series format.")
    seen_ids: set[str] = set()
    inventory_pens = {
        pen.identity: pen for pen in artwork.pen_inventory.pens
    }
    for layer in layers:
        if not layer.records:
            continue
        if layer.id in seen_ids:
            raise MapPlotterError(f"Logical layer ID {layer.id!r} is repeated.")
        seen_ids.add(layer.id)
        if layer.pen_id not in inventory_pens:
            raise MapPlotterError(
                f"{artwork.subject_id}/{layer.id} asks for {layer.pen_id!r}, which "
                f"is absent from pen profile {artwork.pen_inventory.id!r}."
            )
        minimum = 3.0 * layer.pen.mark_width_mm
        for record_index, record in enumerate(layer.records, start=1):
            length = stroke_record_length_mm(record)
            if length + 1e-9 < minimum:
                # A national physical-rail source can contain short, genuine
                # obstruction/tile fragments which must not vanish merely
                # because the whole country is being plotted on A3.  Permit
                # that one evidence-backed case only when the renderer has
                # explicitly ledgered it for pen-proof review.  Every other
                # domain and every unlabelled short stroke retains the binding
                # three-nib quality floor.
                detail_preservation_exception = (
                    artwork.domain == "transit-rail"
                    and record.source_ref is not None
                    and (record.role or "").startswith("physical-rail-")
                    and record.attributes.get("data-three-nib-floor-status")
                    == "below-review-required"
                    and record.attributes.get("data-detail-preservation-exception")
                    == "true"
                )
                if not detail_preservation_exception:
                    raise MapPlotterError(
                        f"{artwork.subject_id}/{layer.id} stroke {record_index} is "
                        f"{length:.3f} mm, below the {minimum:g} mm three-nib floor."
                    )
            min_x, min_y, max_x, max_y = _stroke_record_bounds(record)
            if not (
                artwork.context.safe.left - 0.05 <= min_x
                and max_x <= artwork.context.safe.right + 0.05
                and artwork.context.safe.top - 0.05 <= min_y
                and max_y <= artwork.context.safe.bottom + 0.05
            ):
                raise MapPlotterError(
                    f"{artwork.subject_id}/{layer.id} leaves the plotter-safe area "
                    f"with bounds ({min_x:.3f}, {min_y:.3f})–"
                    f"({max_x:.3f}, {max_y:.3f}) mm."
                )


def _ordered_pen_ids(
    layers: Sequence[ArtworkLayer], preferred_order: Sequence[str] = ()
) -> list[str]:
    present = {layer.pen_id for layer in layers if layer.records}
    order = tuple(preferred_order) or PEN_ORDER
    if len(order) != len(set(order)):
        raise MapPlotterError("A plate pen order cannot repeat a physical pen ID.")
    unknown = sorted(present - set(order))
    if unknown:
        raise MapPlotterError(f"No stable plot order exists for pens: {unknown}.")
    return [pen_id for pen_id in order if pen_id in present]


def _path_attributes(record: StrokeRecord, logical_layer: str) -> dict[str, str]:
    attributes = {
        "d": (
            _physical_vector_path_data(record.vector_path)
            if record.vector_path is not None
            else path_data(record.points)
        ),
        "data-logical-layer": logical_layer,
    }
    if record.source_ref:
        attributes["data-source-ref"] = record.source_ref
    if record.role:
        attributes["data-role"] = record.role
    if record.sequence is not None:
        attributes["data-sequence"] = str(record.sequence)
    attributes.update(record.attributes)
    return attributes


def _physical_vector_path_data(path: VectorPath) -> str:
    """Serialize exact path topology on the binding 0.001 mm SVG grid."""

    def point(value: Point) -> str:
        return f"{format_number(value[0])},{format_number(value[1])}"

    chunks = [f"M {point(path.start)}"]
    for segment in path.segments:
        if isinstance(segment, LineSegment):
            chunks.append(f"L {point(segment.to)}")
        elif isinstance(segment, CubicSegment):
            chunks.append(
                f"C {point(segment.control_1)} {point(segment.control_2)} "
                f"{point(segment.to)}"
            )
        else:  # pragma: no cover - VectorPath validation makes this unreachable.
            raise MapPlotterError("Vector path contains an unsupported segment.")
    if path.closed:
        chunks.append("Z")
    return " ".join(chunks)


def _has_visible_north_mark(layers: Sequence[ArtworkLayer]) -> bool:
    """Report a north mark only when its complete path set is emitted."""

    roles = {
        record.role
        for layer in layers
        for record in layer.records
        if record.role is not None
    }
    return any(
        required <= roles
        for required in (
            {"north-arrow", "north-arrow-head", "north-label"},
            {
                "rotated-north-arrow",
                "rotated-north-arrow-head",
                "rotated-north-label",
            },
        )
    )


def _has_visible_scale_bar(layers: Sequence[ArtworkLayer]) -> bool:
    """Report a scale bar only when line, ticks, and physical label exist."""

    roles = {
        record.role
        for layer in layers
        for record in layer.records
        if record.role is not None
    }
    return {"scale-bar", "scale-bar-tick", "scale-label"} <= roles


def _data_snapshot(artwork: PlateArtwork) -> str:
    """Prefer a release record's frozen snapshot over the legacy fallback."""

    record_snapshot = artwork.catalog_record.get("data_snapshot")
    if isinstance(record_snapshot, str) and record_snapshot.strip():
        return record_snapshot.strip()
    return artwork.data_snapshot


def _pen_up_distance(
    records: Sequence[StrokeRecord], start: Point
) -> tuple[float, Point]:
    total = 0.0
    current = start
    for record in records:
        first = record.points[0]
        total += math.hypot(first[0] - current[0], first[1] - current[1])
        current = record.points[-1]
    return total, current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_plate_unchecked(
    artwork: PlateArtwork,
    *,
    generated_at: str | None = None,
) -> tuple[ET.Element, dict[str, Any]]:
    """Compile one subject into an editable physical SVG and plot manifest."""

    generated_at = generated_at or datetime.now(UTC).isoformat()
    logical_layers = [
        *artwork.layers,
        *(_furniture_layers(artwork) if artwork.include_standard_furniture else []),
    ]
    data_snapshot = _data_snapshot(artwork)
    _validate_artwork(artwork, logical_layers)
    pen_ids = _ordered_pen_ids(logical_layers, artwork.pen_order)
    pen_inventory = artwork.pen_inventory
    pens_by_id = {pen.identity: pen for pen in pen_inventory.pens}
    context = artwork.context

    root = ET.Element(
        svg_tag("svg"),
        {
            "width": f"{context.page.width:g}mm",
            "height": f"{context.page.height:g}mm",
            "viewBox": f"0 0 {context.page.width:g} {context.page.height:g}",
            "version": "1.1",
        },
    )
    if artwork.preview_background.casefold() not in {"white", "#fff", "#ffffff"}:
        # CSS page colour is a display aid, not a drawable SVG element.  Strict
        # plot preflight therefore still sees only the physical path layers.
        root.set("style", f"background-color:{artwork.preview_background}")
        root.set("data-preview-background", artwork.preview_background)
    canonical_title = artwork.document_title or artwork.title
    ET.SubElement(root, svg_tag("title")).text = canonical_title
    ET.SubElement(root, svg_tag("desc")).text = (
        f"Pen-plotter {artwork.domain} plate. Every coordinate is a physical "
        "millimetre and every visible mark is an editable vector path."
    )
    metadata_payload = {
        "schema_version": 1,
        "subject_id": artwork.subject_id,
        "artifact_id": artwork.artifact_id,
        "variant_id": artwork.variant_id,
        "domain": artwork.domain,
        "format_id": context.format_id,
        "scale_status": artwork.scale_status,
        "evidence_status": artwork.evidence_status,
        "rights_status": artwork.rights_status,
        "data_snapshot": data_snapshot,
        "sources": list(artwork.sources),
    }
    metadata_collisions = sorted(set(metadata_payload) & set(artwork.svg_metadata))
    if metadata_collisions:
        raise MapPlotterError(
            "Plate SVG metadata cannot replace binding fields: "
            f"{', '.join(metadata_collisions)}."
        )
    metadata_payload.update(copy.deepcopy(artwork.svg_metadata))
    metadata = ET.SubElement(
        root,
        svg_tag("metadata"),
        {
            f"{{{MAP_NS}}}generator": f"city-map-plotter {__version__}",
            f"{{{MAP_NS}}}subject": artwork.subject_id,
            f"{{{MAP_NS}}}artifact": artwork.artifact_id,
            f"{{{MAP_NS}}}domain": artwork.domain,
            f"{{{MAP_NS}}}pen-profile": pen_inventory.id,
            f"{{{MAP_NS}}}rights-status": artwork.rights_status,
        },
    )
    metadata.text = json.dumps(metadata_payload, sort_keys=True, separators=(",", ":"))
    ET.SubElement(
        root,
        f"{{{SODIPODI_NS}}}namedview",
        {
            "id": "namedview-mapplot-hike",
            "pagecolor": artwork.preview_background,
            "showborder": "true",
            f"{{{INKSCAPE_NS}}}document-units": "mm",
            f"{{{INKSCAPE_NS}}}showpageshadow": "2",
        },
    )

    layers_manifest: list[dict[str, Any]] = []
    pen_sequence: list[dict[str, Any]] = []
    current: Point = (context.safe.left, context.safe.top)
    total_down = 0.0
    total_up = 0.0
    total_paths = 0

    for step, pen_id in enumerate(pen_ids, start=1):
        pen = pens_by_id[pen_id]
        logical_for_pen = [
            layer
            for layer in logical_layers
            if layer.records and layer.pen_id == pen_id
        ]
        all_records = [record for layer in logical_for_pen for record in layer.records]
        path_count = len(all_records)
        length_mm = sum(stroke_record_length_mm(record) for record in all_records)
        pen_up_mm, current = _pen_up_distance(all_records, current)
        total_paths += path_count
        total_down += length_mm
        total_up += pen_up_mm
        preview = pen.preview_color or "#18181b"
        logical_label = ", ".join(layer.label for layer in logical_for_pen[:3])
        if len(logical_for_pen) > 3:
            logical_label += f" +{len(logical_for_pen) - 3}"
        group_label = f"{step:02d} — {logical_label} — {pen.label}"
        group_id = f"layer-pen-{pen_id}"
        group = ET.SubElement(
            root,
            svg_tag("g"),
            {
                "id": group_id,
                f"{{{INKSCAPE_NS}}}groupmode": "layer",
                f"{{{INKSCAPE_NS}}}label": group_label,
                "fill": "none",
                "stroke": preview,
                "stroke-width": format_measurement(pen.mark_width_mm),
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
                "data-pen-step": str(step),
                **physical_group_attributes(
                    ink=pen.ink,
                    nib_mm=pen.mark_width_mm,
                    nominal_nib_mm=pen.nominal_nib_mm,
                    strokes=1,
                    passes=1,
                    plotted_width_mm=pen.mark_width_mm,
                    requested_width_mm=pen.mark_width_mm,
                    width_fit_error_mm=0.0,
                    offset_pitch_mm=0.0,
                    width_fit_mode="single-nib",
                    pen_profile=pen_inventory.id,
                    pen_id=pen.identity,
                    calibration_state=pen.calibration_state,
                    calibration_substrate=pen.substrate,
                ),
            },
        )
        ET.SubElement(
            group, svg_tag("title")
        ).text = (
            f"Step {step}: load {pen.label} ({pen.identity}); plot {path_count} paths."
        )
        for logical in logical_for_pen:
            subgroup = ET.SubElement(
                group,
                svg_tag("g"),
                {
                    "id": f"logical-{logical.id}",
                    "data-logical-layer": logical.id,
                    "data-logical-label": logical.label,
                },
            )
            for record in logical.records:
                ET.SubElement(
                    subgroup,
                    svg_tag("path"),
                    _path_attributes(record, logical.id),
                )

        layer_record = {
            "id": f"pen_{pen_id.replace('-', '_')}",
            "logical_layers": [layer.id for layer in logical_for_pen],
            "label": logical_label,
            "pen": pen.label,
            "ink": pen.ink,
            "nib_mm": pen.mark_width_mm,
            "nominal_nib_mm": pen.nominal_nib_mm,
            "strokes": 1,
            "passes": 1,
            "plotted_width_mm": pen.mark_width_mm,
            "requested_width_mm": pen.mark_width_mm,
            "width_fit_error_mm": 0.0,
            "offset_pitch_mm": 0.0,
            "width_fit_mode": "single-nib",
            "preview_color": preview,
            "preview_stroke_width_mm": pen.mark_width_mm,
            "path_count": path_count,
            "pen_down_distance_mm": round(length_mm, 1),
            "emitted": True,
            "svg_group_id": group_id,
            "svg_layer_label": group_label,
            "pen_profile": pen_inventory.id,
            "pen_id": pen.identity,
            "calibration_state": pen.calibration_state,
            "calibration_substrate": pen.substrate,
            "pen_step": step,
        }
        layers_manifest.append(layer_record)
        pen_sequence.append(
            {
                "step": step,
                "pen": pen.label,
                "pen_id": pen.identity,
                "pen_profile": pen_inventory.id,
                "ink": pen.ink,
                "nib_mm": pen.mark_width_mm,
                "nominal_nib_mm": pen.nominal_nib_mm,
                "calibration_state": pen.calibration_state,
                "calibration_substrate": pen.substrate,
                "preview_color": preview,
                "layers": [layer.id for layer in logical_for_pen],
                "path_count": path_count,
                "pen_down_distance_mm": round(length_mm, 1),
                "pen_up_travel_mm": round(pen_up_mm, 1),
                "strokes": 1,
                "passes": 1,
                "plotted_width_mm": pen.mark_width_mm,
                "minimum_plot_seconds": round(length_mm / 35.0, 1),
                "estimated_plot_seconds_including_pen_up": round(
                    length_mm / 35.0 + pen_up_mm / 80.0,
                    1,
                ),
                "instruction": (
                    f"Load {pen.label} ({pen.identity}) and plot SVG layer {step:02d}."
                ),
                "pen_up_schedule_scope": (
                    "Exact emitted path order; includes travel from the previous "
                    "path endpoint and excludes manual pen-change time."
                ),
            }
        )

    page_zones = {name: rect.as_dict() for name, rect in context.zones.items()}
    if artwork.domain in CIRCUIT_DOMAINS:
        page_zones.update(copy.deepcopy(context.plate.get("circuit_zones_mm") or {}))
    field_area = context.field.width * context.field.height
    field_ink = 0.0
    for layer in logical_layers:
        # The binding validator performs exact clipping.  Domain layers are
        # authored inside the field and furniture is outside, except the frame.
        if layer.id in {"plate_heavy", "plate_copy", "plate_attribution"}:
            continue
        field_ink += sum(
            stroke_record_length_mm(record) * layer.pen.mark_width_mm
            for record in layer.records
        )
    coverage = field_ink / field_area
    warnings = [
        "REVIEW OUTPUT ONLY — the built-in pen inventory contains nominal, "
        "unmeasured widths; calibrate the exact pens, stock, and speed before plotting."
    ]
    rights_cleared = artwork.rights_status in {"commercial-clear", "project-authored"}
    if not rights_cleared:
        if artwork.domain == "architecture":
            rights_warning = (
                "RIGHTS REVIEW REQUIRED — this study does not imply affiliation "
                "with or endorsement by a depicted owner, venue, institution, "
                "designer, or occupant."
            )
        elif artwork.domain == "bridges":
            rights_warning = (
                "RIGHTS REVIEW REQUIRED — source access and a technical-study "
                "credit do not imply affiliation with or endorsement by a bridge "
                "owner, operator, designer, authority, or rights holder."
            )
        elif artwork.domain == "golf":
            rights_warning = (
                "RIGHTS REVIEW REQUIRED — this source-derived course study does "
                "not imply affiliation with or endorsement by a club, venue, "
                "championship, architect, governing body, or rights holder."
            )
        elif artwork.domain == "academic":
            rights_warning = (
                "RIGHTS REVIEW REQUIRED — customer-supplied academic content "
                "must be user-owned, author-supplied, openly licensed, public "
                "domain, institutionally permitted, or otherwise cleared; the "
                "plate does not imply university, journal, or laboratory endorsement."
            )
        elif artwork.domain == "technical-objects":
            rights_warning = (
                "RIGHTS REVIEW REQUIRED — this source-qualified object study "
                "does not imply affiliation with or endorsement by a maker, "
                "owner, team, operator, service, or rights holder."
            )
        else:
            rights_warning = (
                "RIGHTS REVIEW REQUIRED — the plate is a product-development "
                "example, not a representation of league, team, athlete, agency, "
                "or trail endorsement."
            )
        warnings.append(rights_warning)
    rendering = {
        "preset": artwork.rendering_preset,
        "pen_profile": pen_inventory.id,
        "pen_inventory": pen_inventory.as_dict(),
        "allowed_nominal_nibs_mm": context.plate["nib_ladder_mm"],
        "subject_policy": artwork.format_subject_policy or artwork.subject_kind,
        "geometry_is_clipped": True,
        "document_layer_order": "physical-pen-contiguous",
        "empty_layers_omitted_from_svg": True,
        "visible_attribution": artwork.visible_attribution,
        "scale_bar": _has_visible_scale_bar(logical_layers),
        "north_mark": _has_visible_north_mark(logical_layers),
        "stock_tone": artwork.stock_tone,
    }
    collisions = sorted(set(rendering) & set(artwork.rendering_metadata))
    if collisions:
        raise MapPlotterError(
            "Plate rendering metadata cannot replace binding fields: "
            f"{', '.join(collisions)}."
        )
    rendering.update(copy.deepcopy(artwork.rendering_metadata))
    rights = {
        "status": artwork.rights_status,
        "no_endorsement": True,
        "logos_or_trade_dress_used": False,
        "broadcast_frames_traced": False,
    }
    rights_collisions = sorted(set(rights) & set(artwork.rights_metadata))
    if rights_collisions:
        permitted_overrides = {
            "logos_or_trade_dress_used",
            "broadcast_frames_traced",
        }
        forbidden = sorted(set(rights_collisions) - permitted_overrides)
        if forbidden:
            raise MapPlotterError(
                "Plate rights metadata cannot replace binding fields: "
                f"{', '.join(forbidden)}."
            )
    rights.update(copy.deepcopy(artwork.rights_metadata))
    manifest = {
        "schema_version": 2,
        "generator": f"city-map-plotter {__version__}",
        "generated_at": generated_at,
        "artifact_kind": artwork.artifact_kind,
        "artifact_id": artwork.artifact_id,
        "subject_id": artwork.subject_id,
        "variant_id": artwork.variant_id,
        "data_snapshot": data_snapshot,
        "domain": artwork.domain,
        "subject_kind": artwork.subject_kind,
        "title": canonical_title,
        "subtitle": artwork.subtitle,
        "details": list(artwork.details),
        "source": {
            "provider": artwork.source_provider,
            "license": artwork.source_license,
            "attribution": artwork.credit_line,
            "timestamp": data_snapshot,
        },
        "sources": list(artwork.sources),
        "evidence": {
            "scale_status": artwork.scale_status,
            "evidence_status": artwork.evidence_status,
            "notes": list(artwork.notes),
        },
        "rights": rights,
        "page": {
            "paper": context.plate["sheet"],
            "orientation": context.plate["orientation"],
            "width_mm": context.page.width,
            "height_mm": context.page.height,
            "margin_mm": context.plate["safe_margin_mm"],
            "map_bounds_mm": context.field.as_dict(),
            "zones_mm": page_zones,
            "format_id": context.format_id,
            "title_line_layout": copy.deepcopy(
                context.plate["rules"]["title_line_layout"]
            ),
        },
        "rendering": rendering,
        "layers": layers_manifest,
        "pen_sequence": pen_sequence,
        "production_readiness": {
            "production_ready": False,
            "mode": "review-only",
            "selected_pen_ids": pen_ids,
            "uncalibrated_pen_ids": pen_ids,
            "blocking_reasons": [
                "nominal pen widths are not calibrated to one exact paper stock and speed",
                *(
                    []
                    if rights_cleared
                    else ["commercial rights/clearance review is incomplete"]
                ),
                *(
                    []
                    if rendering.get("source_geometry_release_ready", True)
                    else [
                        str(reason)
                        for reason in rendering.get(
                            "source_geometry_release_blockers",
                            ["source geometry review is incomplete"],
                        )
                    ]
                ),
            ],
        },
        "plot_summary": {
            "physical_pen_steps": len(pen_sequence),
            "pen_changes": max(0, len(pen_sequence) - 1),
            "pen_down_path_count": total_paths,
            "pen_down_distance_mm": round(total_down, 1),
            "pen_up_travel_mm": round(total_up, 1),
            "travel_ratio": round(total_up / total_down, 3) if total_down else None,
            "estimated_plot_seconds_including_pen_up": round(
                total_down / 35.0 + total_up / 80.0,
                1,
            ),
            "field_ink_mm2_upper_bound": round(field_ink, 1),
            "field_ink_coverage_upper_bound": round(coverage, 6),
            "field_ink_budget": float(context.plate["ink_budget"]["max_coverage"]),
        },
        "warnings": warnings,
        "catalog_record": artwork.catalog_record,
        "outputs": {},
    }
    return root, manifest


_RELEASE_SNAPSHOT_GENERATED_AT = "2000-01-01T00:00:00+00:00"
_RELEASE_SNAPSHOT_PLACEHOLDER = "0" * 64


def _technical_artwork_release_sha256(artwork: PlateArtwork) -> str:
    """Hash the exact SVG/manifest state of a technical release candidate."""

    candidate = copy.deepcopy(artwork)
    candidate.rendering_metadata["technical_release_artwork_sha256"] = (
        _RELEASE_SNAPSHOT_PLACEHOLDER
    )
    candidate.rendering_metadata["named_source_release_artwork_sha256"] = (
        _RELEASE_SNAPSHOT_PLACEHOLDER
    )
    root, manifest = _render_plate_unchecked(
        candidate,
        generated_at=_RELEASE_SNAPSHOT_GENERATED_AT,
    )
    payload = {
        "svg": ET.tostring(root, encoding="unicode"),
        "manifest": manifest,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_technical_release_authorized(artwork: PlateArtwork) -> None:
    is_technical_artwork = (
        artwork.domain == "technical-objects"
        or artwork.artifact_kind == "engineered-object-plate"
        or artwork.rendering_preset.startswith("technical-object-")
        or artwork.catalog_record.get("kind") == "technical-object"
    )
    if not is_technical_artwork:
        return
    from .technical_source_audit import (
        is_approved_unbound_demonstrator,
        match_named_subject_identity,
        validate_named_subject_release_bindings,
    )

    approved_demonstrator = is_approved_unbound_demonstrator(artwork.catalog_record)
    identity_probe = copy.deepcopy(artwork.catalog_record)
    identity_probe["id"] = artwork.subject_id
    identity_probe["title"] = artwork.title
    identity_probe["document_title"] = artwork.document_title
    identity_match = match_named_subject_identity(identity_probe)
    release_authorized = artwork.rendering_metadata.get("technical_release_authorized")
    release_mode = artwork.rendering_metadata.get("technical_release_mode")
    binding_path = artwork.rendering_metadata.get("technical_release_binding_path")
    snapshot_sha256 = artwork.rendering_metadata.get("technical_release_artwork_sha256")
    if (
        release_authorized is not True
        or not isinstance(snapshot_sha256, str)
        or len(snapshot_sha256) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_sha256)
    ):
        raise MapPlotterError(
            f"Technical-object record {artwork.subject_id!r} is an in-memory "
            "review only and has no verified release authorization; no "
            "serializable plate may be rendered."
        )
    if approved_demonstrator:
        if release_mode != "built-in-demonstrator" or binding_path is not None:
            raise MapPlotterError(
                f"Built-in demonstrator {artwork.subject_id!r} has inconsistent "
                "release metadata."
            )
    else:
        if identity_match is None:
            raise MapPlotterError(
                f"Technical-object record {artwork.subject_id!r} is not an exact "
                "code-reviewed built-in demonstrator and canonical v2 bindings "
                "are not supported for unknown or owner-supplied subjects."
            )
        canonical_subject_id, collection = identity_match
        if release_mode != "v2-binding" or not isinstance(binding_path, str):
            raise MapPlotterError(
                f"Known named {collection} subject {canonical_subject_id!r} is "
                "an in-memory review only and has no activated v2 release binding."
            )
        if artwork.subject_id != canonical_subject_id:
            raise MapPlotterError(
                f"Named {collection} identity {canonical_subject_id!r} cannot render "
                f"under alternate artifact id {artwork.subject_id!r}."
            )
        if (
            artwork.rendering_metadata.get("named_source_release_collection")
            != collection
            or artwork.rendering_metadata.get("named_source_release_subject_id")
            != canonical_subject_id
            or artwork.rendering_metadata.get("named_source_release_authorized")
            is not True
            or artwork.rendering_metadata.get("named_source_release_binding_path")
            != binding_path
        ):
            raise MapPlotterError(
                f"Named {collection} subject {canonical_subject_id!r} has "
                "inconsistent release identity metadata."
            )
        authorizations = validate_named_subject_release_bindings(
            [artwork.catalog_record],
            [binding_path],
        )
        if canonical_subject_id not in authorizations:
            raise MapPlotterError(
                f"Named {collection} subject {canonical_subject_id!r} did not "
                "receive a validator-produced release authorization."
            )

    actual_snapshot_sha256 = _technical_artwork_release_sha256(artwork)
    if actual_snapshot_sha256 != snapshot_sha256:
        raise MapPlotterError(
            f"Technical-object artwork {artwork.subject_id!r} changed after "
            "release validation; render/export is blocked."
        )


def render_plate(
    artwork: PlateArtwork,
    *,
    generated_at: str | None = None,
) -> tuple[ET.Element, dict[str, Any]]:
    """Compile one authorised subject into an editable SVG and manifest."""

    _assert_technical_release_authorized(artwork)
    return _render_plate_unchecked(artwork, generated_at=generated_at)


def _rasterize(
    svg_path: Path,
    png_path: Path,
    *,
    dpi: float,
    background: str = "white",
) -> None:
    inkscape = shutil.which("inkscape")
    if inkscape is None:
        raise MapPlotterError("PNG export requires Inkscape on PATH.")
    result = subprocess.run(
        [
            inkscape,
            str(svg_path),
            "--export-type=png",
            "--export-area-page",
            f"--export-dpi={dpi:g}",
            f"--export-background={background}",
            "--export-background-opacity=255",
            f"--export-filename={png_path}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MapPlotterError(f"Inkscape PNG export failed: {detail}.")


def _pen_only_tree(root: ET.Element, group_id: str) -> ET.Element:
    result = copy.deepcopy(root)
    for child in list(result):
        if (
            child.tag == svg_tag("g")
            and child.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer"
            and child.get("id") != group_id
        ):
            result.remove(child)
    return result


def write_plate(
    artwork: PlateArtwork,
    output_dir: Path,
    *,
    png: bool = True,
    png_dpi: float = 180.0,
    split_pens: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Write the master, manifest, preview, and optional one-pen job files."""

    # Do this before creating the target directory. The same guard is also in
    # render_plate so callers cannot bypass it by serialising the returned XML.
    _assert_technical_release_authorized(artwork)
    output_dir.mkdir(parents=True, exist_ok=True)
    root, manifest = render_plate(artwork, generated_at=generated_at)
    ET.indent(root, space="  ")
    artifact_id = artwork.artifact_id
    svg_path = output_dir / f"{artifact_id}.svg"
    manifest_path = output_dir / f"{artifact_id}.plot.json"
    ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)

    pen_files: list[dict[str, Any]] = []
    if split_pens:
        for record in manifest["pen_sequence"]:
            step = int(record["step"])
            pen_id = str(record["pen_id"])
            pen_path = output_dir / (f"{artifact_id}.pen-{step:02d}-{pen_id}.svg")
            pen_root = _pen_only_tree(root, f"layer-pen-{pen_id}")
            ET.indent(pen_root, space="  ")
            ET.ElementTree(pen_root).write(
                pen_path,
                encoding="utf-8",
                xml_declaration=True,
            )
            pen_files.append(
                {
                    "step": step,
                    "pen": record["pen"],
                    "pen_id": pen_id,
                    "path": str(pen_path.resolve()),
                    "sha256": _sha256(pen_path),
                }
            )

    output_record: dict[str, Any] = {
        "svg": {
            "path": str(svg_path.resolve()),
            "sha256": _sha256(svg_path),
        },
        "manifest": {"path": str(manifest_path.resolve())},
        "pen_files": pen_files,
    }
    if png:
        png_path = output_dir / f"{artifact_id}.png"
        _rasterize(
            svg_path,
            png_path,
            dpi=png_dpi,
            background=artwork.preview_background,
        )
        output_record["png"] = {
            "path": str(png_path.resolve()),
            "dpi": png_dpi,
            "sha256": _sha256(png_path),
        }
    manifest["outputs"] = output_record
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    output_record["manifest"]["sha256"] = _sha256(manifest_path)
    return output_record


def clone_layer(layer: ArtworkLayer) -> ArtworkLayer:
    return copy.deepcopy(layer)


def series_sha256(paths: Iterable[Path]) -> dict[str, str]:
    return {str(path): _sha256(path) for path in paths}
