"""Everything a plate draws that is not map linework.

Border, frame, title, subtitle, detail lines, legend, scale bar, north mark and
attribution.  Every item is placed inside a **named zone** of the selected
``format-v1`` plate, and which zone an item occupies is a theme decision, not a
renderer decision: a theme may put the title in the foot band or the details in
a landscape rail simply by naming a different zone.

``svg.py`` owns the map field.  This module owns the paper around it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from math import floor, log10
from typing import Any, Iterable, Sequence
from xml.etree import ElementTree as ET

from .display_font import display_font_contract, display_text, display_text_width_mm
from .geometry import (
    A5_POSTER_PRESETS,
    Layout,
    Rect,
    load_plate_format,
    polyline_length,
)
from .models import BoundingBox, MapPlotterError, Page
from .pens import DEFAULT_OFFSET_PITCH_RATIO, PenInventory, PenWidthFit
from .stroke_font import normalise_text, stroke_text, text_width_mm, text_width_units
from .svgkit import (
    INKSCAPE_NS,
    Stroke,
    append_vector_strokes,
    decoration_pen_plan,
    format_measurement,
    format_number,
    layer_stats as layer_stats_record,
    path_bounds,
    path_data,
    path_geometry_sha256,
    physical_group_attributes,
    reliable_vector_strokes,
    stroke_geometry_sha256,
    svg_tag,
)
from .textweight import (
    weight_bleed_mm,
    weighted_glyph_strokes,
    weighted_mark_width_mm,
)


POSTER_TITLE_HEIGHT_MM = 7.0
MIN_POSTER_TITLE_HEIGHT_MM = 4.0

#: Horizontal alignments a theme may request inside a zone.
ALIGNMENTS = ("left", "centre", "right", "split")
#: Vertical placements a theme may request inside a zone.
VERTICAL_PLACEMENTS = ("top", "middle", "bottom")
#: Border treatments, matching ``format-v1`` ``border.styles``.
BORDER_STYLES = ("none", "hairline", "double", "rule", "corner")
#: Where the north mark may sit.
NORTH_MARK_PLACEMENTS = ("none", "field-north-east", "field-north-west")

#: Every non-map layer this module can emit, in canonical document order.
FURNITURE_LAYER_IDS = (
    "poster_border",
    "frame",
    "map_furniture",
    "poster_title",
    "poster_subtitle",
    "poster_details",
    "attribution",
)

#: Typography role -> the physical layer that carries its pen.
ROLE_PHYSICAL_LAYERS = {
    "title": "poster_title",
    "subtitle": "poster_subtitle",
    "detail": "poster_details",
    "legend": "map_furniture",
    "attribution": "attribution",
}
#: Typography role -> the SVG group id it is emitted into.
ROLE_GROUP_IDS = {
    "title": "layer-poster_title",
    "subtitle": "layer-poster_subtitle",
    "detail": "layer-poster_details",
    "legend": "layer-map_furniture",
    "attribution": "layer-attribution",
}

#: Rows the crew block is sized for, matching ``CREW_BLOCK_ROWS`` in
#: ``tools/build_format_spec.py``. An eight plus cox is the largest crew.
CREW_BLOCK_ROWS = 9

#: The block body is set slightly larger than the plate's detail size, matching
#: ``CREW_BLOCK_BODY`` in ``tools/build_format_spec.py``.
CREW_BLOCK_BODY = 1.15

ATTRIBUTION_TEXT = "Map data: OpenStreetMap contributors"
ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"


# --------------------------------------------------------------------------
# Placement policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TextPlacement:
    """How one typography role is set inside one named zone."""

    role: str
    zone: str
    font_role: str
    align: str
    vertical: str
    case: str
    max_lines: int
    cap_height_mm: float
    minimum_cap_height_mm: float
    inset_mm: float
    weight_strokes: int = 1
    #: How far the outermost weight offset sits beyond the glyph path. Bold
    #: lettering is wider than the line it is measured from, and the zone has
    #: to give that width back or the ink crosses the boundary.
    bleed_mm: float = 0.0

    def cased(self, value: str) -> str:
        return value.upper() if self.case == "upper" else value


@dataclass(frozen=True)
class FurniturePolicy:
    """The complete non-map arrangement for one plate."""

    border_style: str = "double"
    north_mark: str = "field-north-east"
    scale_bar: bool = True
    divider_rule: bool = True
    placements: dict[str, TextPlacement] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=dict)
    inks: dict[str, str] = field(default_factory=dict)
    nib_roles: dict[str, str] = field(default_factory=dict)
    preview_colors: dict[str, str] = field(default_factory=dict)
    weights: dict[str, int] = field(default_factory=dict)

    def draws(self, layer_id: str) -> bool:
        return bool(self.enabled.get(layer_id, True))

    def ink(self, layer_id: str) -> str:
        return self.inks.get(layer_id, "Black")

    def nib_role(self, layer_id: str, default: str) -> str:
        return self.nib_roles.get(layer_id, default)

    def preview_color(self, layer_id: str) -> str:
        return self.preview_colors.get(layer_id, "#17212b")

    def weight(self, layer_id: str) -> int:
        return int(self.weights.get(layer_id, 1))

    def placement(self, role: str) -> TextPlacement | None:
        return self.placements.get(role)


def _zone(layout: Layout, name: str, *, role: str) -> Rect:
    try:
        return layout.zones[name]
    except KeyError as exc:
        known = ", ".join(sorted(layout.zones))
        raise MapPlotterError(
            f"Furniture role {role!r} names zone {name!r}, which this plate does "
            f"not define. Known zones: {known}."
        ) from exc


def with_split_zones(layout: Layout) -> Layout:
    """Publish the plate's optional column splits alongside its default zones.

    ``split_zones_mm`` is generated by ``tools/build_format_spec.py`` from the
    same head and foot bands the stack already uses, so a theme that names
    ``head_rail`` is still drawing inside the spec.  The map field is untouched,
    which is why this can be merged in without re-projecting anything.
    """

    if layout.format_id is None:
        return layout
    splits = load_plate_format(layout.format_id).get("split_zones_mm")
    if not splits:
        return layout
    extra = {
        name: Rect(
            float(rect["x"]),
            float(rect["y"]),
            float(rect["width"]),
            float(rect["height"]),
        )
        for name, rect in splits.items()
        if name not in layout.zones
    }
    return replace(layout, zones={**layout.zones, **extra}) if extra else layout


def layout_plate_format(layout: Layout) -> dict[str, Any]:
    """Resolve the binding format carried by a poster layout.

    Poster decoration must never infer a format from a preset name: doing so
    makes a larger sheet silently inherit A5 type and nib tables.
    """

    if layout.format_id is None:
        raise MapPlotterError(
            f"Poster layout {layout.preset!r} has no binding plate format id."
        )
    return load_plate_format(layout.format_id)


def default_furniture_policy(layout: Layout) -> FurniturePolicy:
    """The classic stack: everything in the zone that shares its name."""

    plate = layout_plate_format(layout)
    type_scale = plate["type_scale_mm"]
    minimum_caps = plate["rules"]["min_cap_height_mm"]
    gap_mm = float(plate["gap_mm"])
    # Gutters in plate gap units, so an A4 or A3 sheet keeps the proportion.
    gutters = {"title": 2.7, "subtitle": 4.0, "detail": 3.3}
    placements = {}
    for role, zone_name, font_role, align, vertical, case, max_lines in (
        ("title", "title", "display", "centre", "top", "upper", 2),
        ("subtitle", "subtitle", "text", "centre", "middle", "upper", 1),
        ("detail", "detail", "text", "centre", "middle", "upper", 3),
        ("legend", "map_field", "mono", "left", "top", "upper", 1),
        ("attribution", "attribution", "text", "split", "middle", "mixed", 1),
    ):
        placements[role] = TextPlacement(
            role=role,
            zone=zone_name,
            font_role=font_role,
            align=align,
            vertical=vertical,
            case=case,
            max_lines=max_lines,
            cap_height_mm=float(type_scale[role]),
            minimum_cap_height_mm=float(minimum_caps[role]),
            inset_mm=gutters.get(role, 0.0) * gap_mm,
        )
    return FurniturePolicy(
        placements=placements,
        nib_roles=dict(plate["type_nib_role"]),
    )


def furniture_policy_from_contract(contract: dict[str, Any]) -> FurniturePolicy:
    """Build the arrangement a resolved theme contract asks for."""

    typography = contract.get("typography")
    if not isinstance(typography, dict) or not isinstance(
        typography.get("roles"), dict
    ):
        raise MapPlotterError("A design contract must include typography roles.")
    enabled: dict[str, bool] = {}
    inks: dict[str, str] = {}
    nib_roles: dict[str, str] = {}
    preview_colors: dict[str, str] = {}
    weights: dict[str, int] = {}
    effective_nibs: dict[str, float] = {}
    for record in contract.get("resolved_physical_layers", []):
        if not isinstance(record, dict):
            continue
        layer_id = str(record.get("layer_id"))
        enabled[layer_id] = str(record.get("emission")) == "required"
        inks[layer_id] = str(record.get("ink", "Black"))
        nib_roles[layer_id] = str(record.get("nib_role", "text"))
        preview_colors[layer_id] = str(record.get("preview_color", "#17212b"))
        weights[layer_id] = int(record.get("stroke_count", 1))
        effective_nibs[layer_id] = float(record.get("effective_width_mm", 0.25))

    placements: dict[str, TextPlacement] = {}
    for role, record in typography["roles"].items():
        physical_layer_id = str(
            record.get("physical_layer_id", ROLE_PHYSICAL_LAYERS.get(role, ""))
        )
        weight = int(record.get("weight_strokes", weights.get(physical_layer_id, 1)))
        nib_mm = effective_nibs.get(physical_layer_id, 0.25)
        placements[role] = TextPlacement(
            role=str(role),
            zone=str(record["zone"]),
            font_role=str(record["font_role"]),
            align=str(record.get("align", "centre")),
            vertical=str(record.get("vertical", "middle")),
            case=str(record["case"]),
            max_lines=int(record["max_lines"]),
            cap_height_mm=float(record["preferred_cap_height_mm"]),
            minimum_cap_height_mm=float(record["minimum_cap_height_mm"]),
            inset_mm=float(record.get("inset_mm", 0.0)),
            weight_strokes=weight,
            bleed_mm=weight_bleed_mm(nib_mm=nib_mm, stroke_count=weight),
        )
    decoration = contract.get("decoration")
    decoration = decoration if isinstance(decoration, dict) else {}
    return FurniturePolicy(
        border_style=str(decoration.get("border_style", "double")),
        north_mark=str(decoration.get("north_mark", "field-north-east")),
        scale_bar=bool(decoration.get("scale_bar", True)),
        divider_rule=bool(decoration.get("divider_rule", True)),
        placements=placements,
        enabled=enabled,
        inks=inks,
        nib_roles=nib_roles,
        preview_colors=preview_colors,
        weights=weights,
    )


# --------------------------------------------------------------------------
# Typesetting inside a zone
# --------------------------------------------------------------------------


def usable_zone(zone: Rect, placement: TextPlacement) -> Rect:
    """The rectangle the ink may actually occupy.

    The gutter is a design choice; the bleed is physics -- a three-stroke title
    puts ink a millimetre either side of the path the glyph was measured on, and
    that has to come out of the zone or the plate fails its own bounds check.
    """

    inset = placement.inset_mm / 2 + placement.bleed_mm
    pad = placement.bleed_mm
    return Rect(
        zone.x_mm + inset,
        zone.y_mm + pad,
        max(zone.width_mm - 2 * inset, 0.0),
        max(zone.height_mm - 2 * pad, 0.0),
    )


def _anchor_x(usable: Rect, placement: TextPlacement, width_mm: float) -> float:
    if placement.align == "left":
        return usable.x_mm
    if placement.align == "right":
        return usable.x_mm + usable.width_mm - width_mm
    return usable.x_mm + (usable.width_mm - width_mm) / 2


def _block_y(usable: Rect, placement: TextPlacement, block_height_mm: float) -> float:
    if placement.vertical == "top":
        return usable.y_mm
    if placement.vertical == "bottom":
        return usable.y_mm + usable.height_mm - block_height_mm
    return usable.y_mm + (usable.height_mm - block_height_mm) / 2


def _fit_cap(text: str, *, maximum_width_mm: float, cap_height_mm: float) -> float:
    """Shrink a cap height only as far as the available width forces."""

    natural_width = text_width_mm(text, cap_height_mm=cap_height_mm)
    if natural_width <= maximum_width_mm or natural_width <= 0:
        return cap_height_mm
    return cap_height_mm * maximum_width_mm / natural_width


def _two_line_split(
    text: str,
    maximum_width_mm: float,
    *,
    cap_height_mm: float,
) -> tuple[str, str] | None:
    """Choose the most balanced word-boundary split that stays plotter-legible."""

    words = text.split()
    candidates: list[tuple[float, float, int, str, str]] = []
    for split_index in range(1, len(words)):
        first = " ".join(words[:split_index])
        second = " ".join(words[split_index:])
        widths = (
            text_width_mm(first, cap_height_mm=cap_height_mm),
            text_width_mm(second, cap_height_mm=cap_height_mm),
        )
        if max(widths) <= maximum_width_mm:
            candidates.append(
                (max(widths), abs(widths[0] - widths[1]), split_index, first, second)
            )
    if not candidates:
        return None
    _, _, _, first, second = min(candidates)
    return first, second


def fit_title_strokes(
    text: str,
    *,
    centre_x_mm: float,
    y_mm: float,
    maximum_width_mm: float,
    preferred_height_mm: float = POSTER_TITLE_HEIGHT_MM,
    minimum_height_mm: float = MIN_POSTER_TITLE_HEIGHT_MM,
) -> tuple[list[Stroke], int]:
    """Fit a title at a legible size, wrapping once before rejecting it.

    Shared with :func:`set_text_block`; exposed separately so a caller can ask
    "does this title fit?" without owning a plate.
    """

    natural = text_width_mm(text, cap_height_mm=preferred_height_mm)
    if natural <= 0:
        raise MapPlotterError("A title must contain drawable characters.")
    height = min(preferred_height_mm, preferred_height_mm * maximum_width_mm / natural)
    if height + 1e-9 >= minimum_height_mm:
        width = text_width_mm(text, cap_height_mm=height)
        return (
            stroke_text(
                text, x_mm=centre_x_mm - width / 2, y_mm=y_mm, height_mm=height
            ),
            1,
        )
    wrapped = _two_line_split(text, maximum_width_mm, cap_height_mm=minimum_height_mm)
    if wrapped is None:
        raise MapPlotterError(
            f"Poster title {text!r} cannot fit at the minimum legible "
            f"{minimum_height_mm:g} mm cap height. Shorten the title."
        )
    strokes: list[Stroke] = []
    for line_index, line in enumerate(wrapped):
        width = text_width_mm(line, cap_height_mm=minimum_height_mm)
        strokes.extend(
            stroke_text(
                line,
                x_mm=centre_x_mm - width / 2,
                y_mm=y_mm + line_index * (minimum_height_mm + 1.0),
                height_mm=minimum_height_mm,
            )
        )
    return strokes, 2


@dataclass(frozen=True)
class TextBlock:
    """One typeset role: its strokes, the caps actually used, and its copy."""

    strokes: list[Stroke]
    line_strokes: tuple[tuple[str, float, list[Stroke]], ...]
    cap_height_mm: float
    line_count: int


@dataclass(frozen=True)
class BridgeTypographyLine:
    """One independently verifiable line in the bridge plate's furniture."""

    block_id: str
    line_index: int
    layer_id: str
    role: str
    zone: str
    copy: str
    cap_height_mm: float
    nib_mm: float
    strokes: list[Stroke]
    geometry_sha256: str


@dataclass(frozen=True)
class BridgeFurniturePlan:
    """All non-structural geometry for a source-qualified bridge plate."""

    layout: Layout
    drawing_zone: Rect
    frame_strokes: dict[str, Stroke]
    text_lines: tuple[BridgeTypographyLine, ...]


def set_text_block(
    copy: Sequence[str],
    *,
    layout: Layout,
    placement: TextPlacement,
    line_step_mm: float | None = None,
) -> TextBlock:
    """Typeset one role's copy inside the zone the theme gave it.

    This is the single geometry authority for plate lettering: the emitter and
    the contract verifier both call it, so a themed sheet cannot be checked
    against a second, subtly different implementation of its own layout.
    """

    zone = _zone(layout, placement.zone, role=placement.role)
    usable = usable_zone(zone, placement)
    lines = [placement.cased(value) for value in copy if value]
    if not lines:
        raise MapPlotterError(
            f"Furniture role {placement.role!r} was asked to set no copy."
        )
    if len(lines) > placement.max_lines:
        raise MapPlotterError(
            f"Furniture role {placement.role!r} received {len(lines)} lines; "
            f"its theme permits {placement.max_lines}."
        )
    cap = placement.cap_height_mm
    minimum_cap = placement.minimum_cap_height_mm
    maximum_width = usable.width_mm
    if maximum_width <= 0:
        raise MapPlotterError(
            f"Furniture role {placement.role!r} has no room left in zone "
            f"{placement.zone!r} after its {placement.inset_mm:g} mm gutter and "
            f"{placement.bleed_mm:g} mm weight bleed."
        )

    # A single line that will not fit may wrap once, if the theme allows two
    # lines, before it is rejected outright.
    if len(lines) == 1 and placement.max_lines >= 2:
        if (
            _fit_cap(lines[0], maximum_width_mm=maximum_width, cap_height_mm=cap) + 1e-9
            < minimum_cap
        ):
            wrapped = _two_line_split(
                lines[0], maximum_width, cap_height_mm=minimum_cap
            )
            if wrapped is None:
                raise MapPlotterError(
                    f"Furniture role {placement.role!r} copy {lines[0]!r} cannot "
                    f"fit zone {placement.zone!r} at the minimum legible "
                    f"{minimum_cap:g} mm cap height. Shorten it."
                )
            lines = list(wrapped)
            cap = minimum_cap

    line_caps = [
        _fit_cap(line, maximum_width_mm=maximum_width, cap_height_mm=cap)
        for line in lines
    ]
    for line, used_cap in zip(lines, line_caps):
        if used_cap + 1e-9 < minimum_cap:
            raise MapPlotterError(
                f"Furniture role {placement.role!r} line {line!r} only fits zone "
                f"{placement.zone!r} at {used_cap:.3g} mm, below its physical "
                f"{minimum_cap:g} mm cap-height floor. Shorten it or move the "
                "role to a wider zone."
            )
    step = max(line_caps) + 1.0 if line_step_mm is None else line_step_mm
    block_height = max(line_caps) + step * (len(lines) - 1)
    if block_height > usable.height_mm + 1e-9:
        raise MapPlotterError(
            f"Furniture role {placement.role!r} needs {block_height:.3g} mm of "
            f"height but zone {placement.zone!r} offers {usable.height_mm:.3g} mm. "
            "Use fewer lines, a smaller cap_scale, or a taller zone."
        )
    first_y = _block_y(usable, placement, block_height)
    strokes: list[Stroke] = []
    line_records: list[tuple[str, float, list[Stroke]]] = []
    for index, (line, used_cap) in enumerate(zip(lines, line_caps)):
        width = text_width_mm(line, cap_height_mm=used_cap)
        line_strokes = stroke_text(
            line,
            x_mm=_anchor_x(usable, placement, width),
            y_mm=first_y + index * step,
            height_mm=used_cap,
        )
        line_records.append((line, used_cap, line_strokes))
        strokes.extend(line_strokes)
    return TextBlock(
        strokes=strokes,
        line_strokes=tuple(line_records),
        cap_height_mm=min(line_caps),
        line_count=len(lines),
    )


def _technical_plate_layout(format_id: str) -> Layout:
    """Build the neutral named-zone layout used by non-map technical plates."""

    plate = load_plate_format(format_id)
    page_record = plate["page_mm"]
    page = Page(
        float(page_record["width"]),
        float(page_record["height"]),
        str(plate["sheet"]),
        str(plate["orientation"]),
    )
    zones = {
        name: Rect(
            float(record["x"]),
            float(record["y"]),
            float(record["width"]),
            float(record["height"]),
        )
        for name, record in plate["zones_mm"].items()
    }
    safe_margin = float(plate["safe_margin_mm"])
    zones["outer_border"] = Rect(
        safe_margin,
        safe_margin,
        page.width_mm - 2.0 * safe_margin,
        page.height_mm - 2.0 * safe_margin,
    )
    field = zones["map_field"]
    return Layout(
        page=page,
        bbox=BoundingBox(west=0.0, south=0.0, east=1.0, north=1.0),
        margin_mm=safe_margin,
        footer_mm=0.0,
        map_x_mm=field.x_mm,
        map_y_mm=field.y_mm,
        map_width_mm=field.width_mm,
        map_height_mm=field.height_mm,
        scale_mm_per_m=1.0,
        preset="technical-plate",
        format_id=format_id,
        zones=zones,
    )


def bridge_furniture_plan(
    *,
    format_id: str,
    title: str,
    subtitle: str,
    details: Sequence[str],
    credit_lines: Sequence[str],
    dimension_labels: Sequence[str],
    field_label: str = "SIDE ELEVATION / EQUAL AXES / DIMENSION-LED SCHEMATIC",
) -> BridgeFurniturePlan:
    """Regenerate every non-structural bridge mark from the plate contract.

    Both the bridge emitter and its independent QA call this function. Copy,
    cap heights, named zones, the field subdivision, and geometry digests
    therefore have one authority in ``furniture.py``.
    """

    layout = _technical_plate_layout(format_id)
    plate = layout_plate_format(layout)
    bridge_records = plate.get("bridge_zones_mm")
    if not isinstance(bridge_records, dict):
        raise MapPlotterError(
            "Plate format does not publish the bridge technical-plate zones."
        )
    required_bridge_zones = {
        "bridge_field_label",
        "bridge_drawing",
        "bridge_dimension_label",
    }
    if set(bridge_records) != required_bridge_zones:
        raise MapPlotterError(
            "Plate format bridge_zones_mm must contain exactly: "
            + ", ".join(sorted(required_bridge_zones))
            + "."
        )
    bridge_zones = {
        name: Rect(
            float(record["x"]),
            float(record["y"]),
            float(record["width"]),
            float(record["height"]),
        )
        for name, record in bridge_records.items()
    }
    drawing_zone = bridge_zones["bridge_drawing"]
    if min(drawing_zone.width_mm, drawing_zone.height_mm) <= 0:
        raise MapPlotterError("Binding bridge_drawing zone has no usable area.")
    layout = replace(
        layout,
        zones={
            **layout.zones,
            **bridge_zones,
        },
    )

    lines: list[BridgeTypographyLine] = []

    def add_block(
        *,
        block_id: str,
        layer_id: str,
        role: str,
        zone: str,
        copy_lines: Sequence[str],
        type_role: str,
        align: str,
        vertical: str,
        case: str,
        max_lines: int,
        cap_height_mm: float | None = None,
        line_step_mm: float | None = None,
    ) -> None:
        clean_copy = [str(value).strip() for value in copy_lines if str(value).strip()]
        if not clean_copy:
            raise MapPlotterError(f"Bridge furniture block {block_id!r} has no copy.")
        nib_role = str(plate["type_nib_role"][type_role])
        nib_mm = float(plate["nib_roles_mm"][nib_role])
        minimum_cap = max(
            float(plate["rules"]["min_cap_height_mm"][type_role]),
            8.0 * nib_mm,
        )
        placement = TextPlacement(
            role=role,
            zone=zone,
            font_role="text",
            align=align,
            vertical=vertical,
            case=case,
            max_lines=max_lines,
            cap_height_mm=(
                float(plate["type_scale_mm"][type_role])
                if cap_height_mm is None
                else cap_height_mm
            ),
            minimum_cap_height_mm=minimum_cap,
            inset_mm=0.0,
        )
        block = set_text_block(
            clean_copy,
            layout=layout,
            placement=placement,
            line_step_mm=line_step_mm,
        )
        for line_index, (line_copy, line_cap, raw_strokes) in enumerate(
            block.line_strokes, start=1
        ):
            strokes = reliable_vector_strokes(raw_strokes, nib_mm=nib_mm)
            if not strokes:
                raise MapPlotterError(
                    f"Bridge furniture block {block_id!r} line {line_index} "
                    "has no physical strokes after the three-nib gate."
                )
            lines.append(
                BridgeTypographyLine(
                    block_id=block_id,
                    line_index=line_index,
                    layer_id=layer_id,
                    role=role,
                    zone=zone,
                    copy=line_copy,
                    cap_height_mm=line_cap,
                    nib_mm=nib_mm,
                    strokes=strokes,
                    geometry_sha256=stroke_geometry_sha256(strokes),
                )
            )

    add_block(
        block_id="title",
        layer_id="plate_heavy",
        role="title",
        zone="title",
        copy_lines=[title],
        type_role="title",
        align="centre",
        vertical="middle",
        case="upper",
        max_lines=2,
    )
    add_block(
        block_id="subtitle",
        layer_id="plate_copy",
        role="subtitle",
        zone="subtitle",
        copy_lines=[subtitle],
        type_role="subtitle",
        align="centre",
        vertical="middle",
        case="upper",
        max_lines=1,
    )
    add_block(
        block_id="details",
        layer_id="plate_copy",
        role="detail",
        zone="detail",
        copy_lines=details,
        type_role="detail",
        align="centre",
        vertical="middle",
        case="upper",
        max_lines=int(plate["detail_lines"]),
        line_step_mm=float(plate["type_scale_mm"]["detail"]) + 0.9,
    )
    attribution_nib_role = str(plate["type_nib_role"]["attribution"])
    attribution_nib = float(plate["nib_roles_mm"][attribution_nib_role])
    attribution_cap = min(
        float(plate["type_scale_mm"]["attribution"]),
        (layout.zones["attribution"].height_mm - 3.0 * attribution_nib)
        / max(len(credit_lines), 1),
    )
    add_block(
        block_id="attribution",
        layer_id="plate_attribution",
        role="attribution",
        zone="attribution",
        copy_lines=credit_lines,
        type_role="attribution",
        align="centre",
        vertical="middle",
        case="mixed",
        max_lines=2,
        cap_height_mm=attribution_cap,
        line_step_mm=attribution_cap + 3.0 * attribution_nib,
    )
    add_block(
        block_id="field-panel-label",
        layer_id="bridge_copy",
        role="field-panel-label",
        zone="bridge_field_label",
        copy_lines=[field_label],
        type_role="attribution",
        align="left",
        vertical="top",
        case="upper",
        max_lines=1,
    )
    add_block(
        block_id="dimension-label",
        layer_id="bridge_copy",
        role="dimension-label",
        zone="bridge_dimension_label",
        copy_lines=[" / ".join(str(value) for value in dimension_labels)],
        type_role="attribution",
        align="centre",
        vertical="middle",
        case="upper",
        max_lines=1,
    )

    borders = border_geometry(
        layout.zones["outer_border"],
        style="double",
        inner_offset_mm=float(plate["border"]["inner_offset_mm"]),
    )
    if len(borders) != 2:
        raise MapPlotterError("Bridge technical plate requires a double border.")
    return BridgeFurniturePlan(
        layout=layout,
        drawing_zone=drawing_zone,
        frame_strokes={
            "outer-border": borders[0],
            "inner-border": borders[1],
            "field-frame": _rectangle(layout.zones["map_field"]),
        },
        text_lines=tuple(lines),
    )


def weighted(
    strokes: list[Stroke], *, placement_or_count: int, nib_mm: float
) -> list[Stroke]:
    """Apply type weight with the same parallel-offset engine roads use."""

    return weighted_glyph_strokes(
        strokes, stroke_count=placement_or_count, nib_mm=nib_mm
    )


def type_weight_plan(*, stroke_count: int, nib_mm: float) -> tuple[float, float]:
    """The pitch and achieved stem width for a weighted typography role."""

    pitch_mm = 0.0 if stroke_count == 1 else DEFAULT_OFFSET_PITCH_RATIO * nib_mm
    return pitch_mm, weighted_mark_width_mm(nib_mm=nib_mm, stroke_count=stroke_count)


# --------------------------------------------------------------------------
# Contract binding and evidence
# --------------------------------------------------------------------------


def design_contract_identity(contract: dict[str, Any]) -> tuple[str, str, str]:
    """Validate the stable identities needed for SVG/manifest binding."""

    import re

    theme_id = contract.get("theme_id")
    theme_sha = contract.get("theme_sha256")
    edition_sha = contract.get("edition_signature_sha256")
    if not isinstance(theme_id, str) or not theme_id.strip():
        raise MapPlotterError("A design contract must identify its series theme.")
    for label, value in (("theme", theme_sha), ("edition", edition_sha)):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise MapPlotterError(
                f"A design contract must carry a canonical {label} SHA-256."
            )
    assert isinstance(theme_sha, str) and isinstance(edition_sha, str)
    return theme_id, theme_sha, edition_sha


def bind_design_contract_groups(
    root: ET.Element,
    contract: dict[str, Any],
) -> None:
    """Attach theme/font/zone identity to the SVG and its lettering groups."""

    theme_id, theme_sha, edition_sha = design_contract_identity(contract)
    font = contract.get("font")
    typography = contract.get("typography")
    if not isinstance(font, dict) or not isinstance(typography, dict):
        raise MapPlotterError(
            "A design contract must include font and typography data."
        )
    font_id = font.get("font_id")
    font_sha = font.get("sha256")
    roles = typography.get("roles")
    if (
        not isinstance(font_id, str)
        or not isinstance(font_sha, str)
        or not isinstance(roles, dict)
    ):
        raise MapPlotterError("A design contract has malformed typography identity.")
    root.set("data-series-theme", theme_id)
    root.set("data-series-theme-sha256", theme_sha)
    root.set("data-edition-signature-sha256", edition_sha)
    group_roles = {group_id: role for role, group_id in ROLE_GROUP_IDS.items()}
    for group in root.iter(svg_tag("g")):
        role = group_roles.get(group.get("id", ""))
        if role is None:
            continue
        record = roles.get(role)
        if not isinstance(record, dict):
            raise MapPlotterError(
                f"Design contract typography is missing role {role!r}."
            )
        group.set("data-theme-role", role)
        group.set("data-theme-zone", str(record["zone"]))
        group.set("data-theme-placement", str(record["placement"]))
        group.set("data-theme-align", str(record.get("align", "centre")))
        group.set("data-stroke-font-id", font_id)
        group.set("data-stroke-font-sha256", font_sha)


def theme_role_nib_mm(contract: dict[str, Any], role_contract: dict[str, Any]) -> float:
    """Resolve the exact physical mark assigned to a typography role."""

    physical_layer_id = role_contract.get("physical_layer_id")
    matches = [
        record
        for record in contract.get("resolved_physical_layers", [])
        if isinstance(record, dict) and record.get("layer_id") == physical_layer_id
    ]
    if len(matches) != 1:
        raise MapPlotterError(
            f"Theme typography role has no unique physical layer {physical_layer_id!r}."
        )
    try:
        return float(matches[0]["effective_width_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Theme physical layer {physical_layer_id!r} has no effective nib."
        ) from exc


def _detail_line_step(layout: Layout, placement: TextPlacement) -> float:
    """Leading that puts the plate's full detail-line count in its zone."""

    plate = layout_plate_format(layout)
    usable = usable_zone(_zone(layout, placement.zone, role=placement.role), placement)
    return (usable.height_mm - placement.cap_height_mm) / max(
        1, int(plate["detail_lines"]) - 1
    )


def expected_typography_strokes(
    *,
    role: str,
    source_copy: list[str],
    group: ET.Element,
    role_contract: dict[str, Any],
    contract: dict[str, Any],
    layout: Layout,
) -> tuple[list[Stroke], int]:
    """Rebuild copy from the contract to bind text, font, and placement.

    This intentionally does not trust the SVG's stored geometry digest.  The
    digest only supplies an emission-time integrity witness; these strokes are
    regenerated from source copy, the selected plate, and the resolved nib.
    """

    nib_mm = theme_role_nib_mm(contract, role_contract)
    policy = furniture_policy_from_contract(contract)
    placement = policy.placements[role]
    weight = int(role_contract.get("weight_strokes", 1))

    if role == "legend":
        return _legend_strokes(
            group=group,
            layout=layout,
            placement=placement,
            north_mark=policy.north_mark,
            source_copy=source_copy,
            nib_mm=nib_mm,
            weight_strokes=weight,
        )
    if role == "attribution":
        attribution_strokes = _attribution_block(layout, placement)
        return (
            reliable_vector_strokes(
                weighted(
                    attribution_strokes,
                    placement_or_count=weight,
                    nib_mm=nib_mm,
                ),
                nib_mm=nib_mm,
            ),
            1,
        )

    line_step = _detail_line_step(layout, placement) if role == "detail" else None
    text_block = set_text_block(
        source_copy, layout=layout, placement=placement, line_step_mm=line_step
    )
    return (
        reliable_vector_strokes(
            weighted(
                text_block.strokes,
                placement_or_count=weight,
                nib_mm=nib_mm,
            ),
            nib_mm=nib_mm,
        ),
        text_block.line_count,
    )


def typography_evidence(
    root: ET.Element,
    contract: dict[str, Any],
    layout: Layout,
) -> dict[str, Any]:
    """Verify and record drawable copy, cap height, and named-zone placement."""

    font = contract["font"]
    typography = contract["typography"]
    if any(element.get("transform") is not None for element in root.iter()):
        raise MapPlotterError(
            "Themed artwork must bake every coordinate; SVG transforms make "
            "physical zone evidence ambiguous."
        )
    groups = {
        group.get("id"): group
        for group in root.iter(svg_tag("g"))
        if group.get("id") is not None
    }
    evidence: dict[str, Any] = {}
    for role, group_id in ROLE_GROUP_IDS.items():
        role_contract = typography["roles"][role]
        group = groups.get(group_id)
        if group is None:
            evidence[role] = {**role_contract, "emitted": False}
            continue
        transformed = [
            element for element in group.iter() if element.get("transform") is not None
        ]
        if transformed:
            raise MapPlotterError(
                f"Theme typography role {role!r} uses an SVG transform; named-zone "
                "evidence requires baked physical coordinates."
            )
        if role == "detail":
            paths = [
                path
                for path in group.iter(svg_tag("path"))
                if path.get("data-copy") is not None
            ]
            lines: dict[int, str] = {}
            for path in paths:
                raw_line = path.get("data-detail-line")
                try:
                    line_number = int(raw_line or "")
                except ValueError as exc:
                    raise MapPlotterError(
                        "Theme detail geometry has no numeric line identity."
                    ) from exc
                detail_copy_value = str(path.get("data-copy"))
                previous = lines.setdefault(line_number, detail_copy_value)
                if previous != detail_copy_value:
                    raise MapPlotterError(
                        f"Theme detail line {line_number} carries mixed source copy."
                    )
            if sorted(lines) != list(range(1, len(lines) + 1)):
                raise MapPlotterError(
                    "Theme detail line identities must be contiguous from one."
                )
            source_copy = [lines[line] for line in sorted(lines)]
        elif role == "legend":
            paths = [
                path
                for path in group.iter(svg_tag("path"))
                if path.get("data-north") is not None
                or path.get("data-scale-distance-m") is not None
            ]
            legend_copy_value = group.get("data-copy")
            source_copy = [] if legend_copy_value is None else [legend_copy_value]
        else:
            paths = list(group.iter(svg_tag("path")))
            group_copy_value = group.get("data-copy")
            source_copy = [] if group_copy_value is None else [group_copy_value]
        case = str(role_contract["case"])
        if not source_copy:
            raise MapPlotterError(
                f"Theme typography role {role!r} emitted geometry without its "
                "source-copy evidence."
            )
        cased_copy = [
            value.upper() if case == "upper" else value for value in source_copy
        ]
        drawable_copy = [normalise_text(value) for value in cased_copy]
        line_count = int(
            group.get(
                "data-title-lines" if role == "title" else "data-line-count",
                str(len(source_copy) if source_copy else 1),
            )
        )
        maximum_lines = int(role_contract["max_lines"])
        if line_count < 1 or line_count > maximum_lines:
            raise MapPlotterError(
                f"Theme typography role {role!r} emitted {line_count} lines; "
                f"its contract permits 1-{maximum_lines}."
            )
        expected_strokes, expected_line_count = expected_typography_strokes(
            role=role,
            source_copy=source_copy,
            group=group,
            role_contract=role_contract,
            contract=contract,
            layout=layout,
        )
        if line_count != expected_line_count:
            raise MapPlotterError(
                f"Theme typography role {role!r} line count does not match its "
                "deterministic copy layout."
            )
        expected_geometry_sha = stroke_geometry_sha256(expected_strokes)
        geometry_sha = path_geometry_sha256(paths)
        stored_geometry_sha = group.get("data-copy-geometry-sha256")
        if stored_geometry_sha != expected_geometry_sha:
            raise MapPlotterError(
                f"Theme typography role {role!r} emission digest does not bind "
                "its source copy, font, and placement contract."
            )
        if geometry_sha != expected_geometry_sha:
            raise MapPlotterError(
                f"Theme typography role {role!r} geometry does not match the "
                "deterministic rendering of its source copy."
            )
        cap_values = {
            float(value)
            for value in (
                [group.get("data-cap-height-mm")]
                + [path.get("data-cap-height-mm") for path in paths]
            )
            if value is not None
        }
        if not cap_values:
            raise MapPlotterError(
                f"Theme typography role {role!r} has no emitted cap-height evidence."
            )
        minimum_cap = float(role_contract["minimum_cap_height_mm"])
        if min(cap_values) + 1e-9 < minimum_cap:
            raise MapPlotterError(
                f"Theme typography role {role!r} falls below its "
                f"{minimum_cap:g} mm physical cap-height floor."
            )
        bounds = path_bounds(paths)
        zone_name = str(role_contract["zone"])
        zone = layout.zones.get(zone_name)
        if zone is None:
            raise MapPlotterError(
                f"Theme typography role {role!r} references absent layout zone "
                f"{zone_name!r}."
            )
        if bounds is None:
            raise MapPlotterError(
                f"Theme typography role {role!r} emitted no measurable geometry."
            )
        # The north mark is a map overlay by definition, so its label is checked
        # against the map field while the scale block is checked against the
        # zone the theme actually gave the legend.  Everything else is one zone.
        checks: list[tuple[str, Rect, list[ET.Element]]] = []
        if role == "legend":
            placement = furniture_policy_from_contract(contract).placements.get(role)
            north_paths = [path for path in paths if path.get("data-north") is not None]
            scale_paths = [path for path in paths if path.get("data-north") is None]
            if north_paths:
                checks.append(("map_field", layout.zones["map_field"], north_paths))
            if scale_paths:
                scale_zone = legend_zone(layout, placement)
                scale_zone_name = "furniture" if zone_name == "map_field" else zone_name
                checks.append((scale_zone_name, scale_zone, scale_paths))
        else:
            checks.append((zone_name, zone, paths))
        tolerance = 0.002
        for checked_name, checked_zone, checked_paths in checks:
            checked_bounds = path_bounds(checked_paths)
            if checked_bounds is None:
                continue
            zone_left, zone_top, zone_right, zone_bottom = checked_zone.bounds
            bounds_right = checked_bounds["x"] + checked_bounds["width"]
            bounds_bottom = checked_bounds["y"] + checked_bounds["height"]
            within_zone = (
                checked_bounds["x"] >= zone_left - tolerance
                and checked_bounds["y"] >= zone_top - tolerance
                and bounds_right <= zone_right + tolerance
                and bounds_bottom <= zone_bottom + tolerance
            )
            if not within_zone:
                raise MapPlotterError(
                    f"Theme typography role {role!r} escapes its {checked_name!r} "
                    f"zone: ink spans x {checked_bounds['x']:.3f}-{bounds_right:.3f}, "
                    f"y {checked_bounds['y']:.3f}-{bounds_bottom:.3f} mm against a "
                    f"zone of x {zone_left:.3f}-{zone_right:.3f}, "
                    f"y {zone_top:.3f}-{zone_bottom:.3f} mm."
                )
        evidence[role] = {
            **role_contract,
            "emitted": True,
            "source_copy": source_copy,
            "drawable_copy": drawable_copy,
            "normalisation_changed": cased_copy != drawable_copy,
            "actual_cap_height_mm": (
                next(iter(cap_values)) if len(cap_values) == 1 else sorted(cap_values)
            ),
            "line_count": line_count,
            "geometry_bounds_mm": bounds,
            "geometry_sha256": geometry_sha,
            "copy_geometry_verified": True,
            "zone_bounds_mm": zone.as_dict(),
            "within_zone": True,
            "placement_satisfied": True,
        }
    return {
        "schema_version": 1,
        "policy_id": typography["policy_id"],
        "font": font,
        "roles": evidence,
    }


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def _group(
    root: ET.Element,
    *,
    group_id: str,
    label: str,
    color: str,
    plan: PenWidthFit,
    ink: str,
    profile_id: str,
    stroke_count: int = 1,
    offset_pitch_mm: float = 0.0,
    plotted_width_mm: float | None = None,
    linecap: str = "round",
    linejoin: str = "round",
    extra: dict[str, str] | None = None,
) -> ET.Element:
    plotted = plan.plotted_width_mm if plotted_width_mm is None else plotted_width_mm
    attributes = {
        "id": group_id,
        f"{{{INKSCAPE_NS}}}groupmode": "layer",
        f"{{{INKSCAPE_NS}}}label": label,
        "fill": "none",
        "stroke": color,
        "stroke-width": format_measurement(plan.pen.mark_width_mm),
        "stroke-linecap": linecap,
        "stroke-linejoin": linejoin,
        **physical_group_attributes(
            ink=ink,
            nib_mm=plan.pen.mark_width_mm,
            strokes=stroke_count,
            passes=1,
            plotted_width_mm=plotted,
            nominal_nib_mm=plan.pen.nominal_nib_mm,
            requested_width_mm=plan.requested_width_mm,
            width_fit_error_mm=plotted - plan.requested_width_mm,
            offset_pitch_mm=offset_pitch_mm,
            width_fit_mode=plan.mode,
            pen_profile=profile_id,
            pen_id=plan.pen.id,
            calibration_state=plan.pen.calibration_state,
            calibration_substrate=plan.pen.substrate,
        ),
    }
    attributes.update(extra or {})
    return ET.SubElement(root, svg_tag("g"), attributes)


def _record(
    layer_stats: list[dict[str, Any]],
    *,
    layer_id: str,
    label: str,
    plan: PenWidthFit,
    ink: str,
    color: str,
    profile_id: str,
    path_count: int,
    length_mm: float,
    group_id: str,
    group_label: str,
    stroke_count: int = 1,
    offset_pitch_mm: float = 0.0,
    plotted_width_mm: float | None = None,
) -> None:
    plotted = plan.plotted_width_mm if plotted_width_mm is None else plotted_width_mm
    layer_stats.append(
        layer_stats_record(
            layer_id=layer_id,
            label=label,
            pen=plan.pen.label,
            ink=ink,
            nib_mm=plan.pen.mark_width_mm,
            strokes=stroke_count,
            passes=1,
            color=color,
            plotted_width_mm=plotted,
            path_count=path_count,
            length_mm=length_mm,
            emitted=True,
            svg_group_id=group_id,
            svg_layer_label=group_label,
            nominal_nib_mm=plan.pen.nominal_nib_mm,
            requested_width_mm=plan.requested_width_mm,
            width_fit_error_mm=plotted - plan.requested_width_mm,
            offset_pitch_mm=offset_pitch_mm,
            width_fit_mode=plan.mode,
            pen_profile=profile_id,
            pen_id=plan.pen.id,
            calibration_state=plan.pen.calibration_state,
            calibration_substrate=plan.pen.substrate,
        )
    )


def _total_length(strokes: Iterable[Stroke]) -> float:
    return sum(polyline_length(stroke) for stroke in strokes)


def _rectangle(rect: Rect, inset_mm: float = 0.0) -> list[tuple[float, float]]:
    left = rect.x_mm + inset_mm
    top = rect.y_mm + inset_mm
    right = rect.x_mm + rect.width_mm - inset_mm
    bottom = rect.y_mm + rect.height_mm - inset_mm
    return [(left, top), (right, top), (right, bottom), (left, bottom), (left, top)]


def border_geometry(
    zone: Rect, *, style: str, inner_offset_mm: float, corner_mm: float = 6.0
) -> list[list[tuple[float, float]]]:
    """Return the paths for one of the five specified border treatments."""

    if style not in BORDER_STYLES:
        raise MapPlotterError(
            f"Unknown border style {style!r}. Choose from: {', '.join(BORDER_STYLES)}."
        )
    if style == "none":
        return []
    if style == "hairline":
        return [_rectangle(zone)]
    if style == "double":
        return [_rectangle(zone), _rectangle(zone, inner_offset_mm)]
    left, top, right, bottom = zone.bounds
    if style == "rule":
        return [[(left, top), (right, top)], [(left, bottom), (right, bottom)]]
    return [
        [(left, top + corner_mm), (left, top), (left + corner_mm, top)],
        [(right - corner_mm, top), (right, top), (right, top + corner_mm)],
        [(right, bottom - corner_mm), (right, bottom), (right - corner_mm, bottom)],
        [(left + corner_mm, bottom), (left, bottom), (left, bottom - corner_mm)],
    ]


def coordinate_label(layout: Layout, *, separator: str = " / ") -> str:
    return separator.join(coordinate_lines(layout))


def coordinate_lines(layout: Layout) -> tuple[str, str]:
    """Return latitude and longitude as independently placeable copy lines."""

    latitude, longitude = layout.bbox.center
    return (
        f"{abs(latitude):.4f} {'N' if latitude >= 0 else 'S'}",
        f"{abs(longitude):.4f} {'E' if longitude >= 0 else 'W'}",
    )


def nice_scale_distance(target_m: float) -> float:
    exponent = 10 ** floor(log10(max(target_m, 1e-9)))
    candidates = [value * exponent for value in (1.0, 2.0, 5.0, 10.0)]
    return max(
        (value for value in candidates if value <= target_m), default=candidates[0]
    )


def append_map_frame(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    policy: FurniturePolicy,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> None:
    """Rule the map field's own edge."""

    if not policy.draws("frame"):
        return
    poster = layout.preset in A5_POSTER_PRESETS
    requested_width = (
        float(layout_plate_format(layout)["nib_roles_mm"]["primary"])
        if poster
        else 0.25
    )
    ink = policy.ink("frame")
    plan = decoration_pen_plan(
        ink=ink,
        requested_width_mm=requested_width,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    color = policy.preview_colors.get("frame", "#17212b" if poster else "#18181b")
    label = f"94 — Map frame — {plan.pen.label}"
    group = _group(
        root,
        group_id="layer-frame",
        label=label,
        color=color,
        plan=plan,
        ink=ink,
        profile_id=profile_id,
        linecap="butt",
        linejoin="miter",
    )
    left, top, right, bottom = layout.clip_rect
    frame_points = [
        (left, top),
        (right, top),
        (right, bottom),
        (left, bottom),
        (left, top),
    ]
    ET.SubElement(group, svg_tag("path"), {"d": path_data(frame_points)})
    _record(
        layer_stats,
        layer_id="frame",
        label="Map frame",
        plan=plan,
        ink=ink,
        color=color,
        profile_id=profile_id,
        path_count=1,
        length_mm=2 * (layout.map_width_mm + layout.map_height_mm),
        group_id="layer-frame",
        group_label=label,
    )


def legend_zone(layout: Layout, placement: TextPlacement | None) -> Rect:
    """Where the scale block sits.

    A theme naming ``map_field`` means "north mark only": the map field is not a
    band you can set a scale bar in, so the default furniture band is used
    instead. Every other named zone is taken literally.
    """

    name = "furniture" if placement is None else placement.zone
    if name == "map_field":
        name = "furniture"
    return _zone(layout, name, role="legend")


def scale_block_geometry(
    layout: Layout,
    placement: TextPlacement | None,
    *,
    cap_height_mm: float,
) -> tuple[float, float, float]:
    """Label origin and bar baseline for the scale block, inside its zone."""

    if layout.preset not in A5_POSTER_PRESETS:
        left, _, _, bottom = layout.clip_rect
        return left + 4.0, bottom - 4.0, bottom - 7.0
    zone = legend_zone(layout, placement)
    usable = usable_zone(zone, placement) if placement is not None else zone
    bar_x = usable.x_mm + 4.0
    label_y = usable.y_mm
    bar_y = min(
        usable.y_mm + cap_height_mm + 2.0,
        usable.y_mm + usable.height_mm - 0.5,
    )
    return bar_x, bar_y, label_y


def _legend_strokes(
    *,
    group: ET.Element,
    layout: Layout,
    placement: TextPlacement,
    north_mark: str,
    source_copy: list[str],
    nib_mm: float,
    weight_strokes: int,
) -> tuple[list[Stroke], int]:
    """Rebuild the legend's lettering from its emitted scale-bar state."""

    strokes: list[Stroke] = []
    distance_values = {
        path.get("data-scale-distance-m")
        for path in group.iter(svg_tag("path"))
        if path.get("data-scale-distance-m") is not None
    }
    cap = placement.cap_height_mm
    bar_x, _, label_y = scale_block_geometry(layout, placement, cap_height_mm=cap)
    drew_north = any(
        path.get("data-north") is not None for path in group.iter(svg_tag("path"))
    )
    copy_parts: list[str] = ["N"] if drew_north else []
    if distance_values:
        if len(distance_values) != 1:
            raise MapPlotterError("Theme legend carries inconsistent scale labels.")
        distance_value = next(iter(distance_values))
        assert distance_value is not None
        distance_label = scale_distance_label(float(distance_value))
        strokes.extend(
            reliable_vector_strokes(
                weighted(
                    stroke_text(
                        distance_label, x_mm=bar_x, y_mm=label_y, height_mm=cap
                    ),
                    placement_or_count=weight_strokes,
                    nib_mm=nib_mm,
                ),
                nib_mm=nib_mm,
            )
        )
        copy_parts.append(distance_label)
    expected_copy = " / ".join(copy_parts)
    if source_copy and source_copy[0] != expected_copy:
        raise MapPlotterError(
            f"Theme legend source copy {source_copy[0]!r} does not match its "
            f"emitted scale-bar and north-mark state ({expected_copy!r})."
        )
    if drew_north:
        strokes.extend(
            reliable_vector_strokes(
                weighted(
                    north_label_strokes(
                        layout, cap_height_mm=cap, placement=north_mark
                    ),
                    placement_or_count=weight_strokes,
                    nib_mm=nib_mm,
                ),
                nib_mm=nib_mm,
            )
        )
    return strokes, 1


def scale_distance_label(distance_m: float) -> str:
    return f"{distance_m / 1_000:g} KM" if distance_m >= 1_000 else f"{distance_m:g} M"


def north_mark_geometry(
    layout: Layout, *, placement: str
) -> tuple[list[tuple[float, float]], float, float]:
    left, top, right, _ = layout.clip_rect
    north_x = (left + 5.0) if placement == "field-north-west" else (right - 5.0)
    north_top = top + 4.0
    return (
        [
            (north_x, north_top + 8.0),
            (north_x, north_top),
            (north_x - 1.7, north_top + 2.4),
            (north_x, north_top),
            (north_x + 1.7, north_top + 2.4),
        ],
        north_x,
        north_top,
    )


def north_label_strokes(
    layout: Layout, *, cap_height_mm: float, placement: str = "field-north-east"
) -> list[Stroke]:
    _, north_x, north_top = north_mark_geometry(layout, placement=placement)
    return stroke_text(
        "N",
        x_mm=north_x - 0.75,
        y_mm=north_top - max(3.0, cap_height_mm + 1.0),
        height_mm=cap_height_mm,
    )


def append_map_furniture(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    policy: FurniturePolicy,
    include_scale_bar: bool = True,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> None:
    """Add a north mark and, when requested, a physical distance scale bar."""

    if not policy.draws("map_furniture"):
        return
    include_scale_bar = include_scale_bar and policy.scale_bar
    draw_north = policy.north_mark != "none"
    if not include_scale_bar and not draw_north:
        return
    ink = policy.ink("map_furniture")
    plate = layout_plate_format(layout) if layout.format_id is not None else None
    requested_nib_mm = (
        float(plate["nib_roles_mm"][policy.nib_role("map_furniture", "text")])
        if plate is not None
        else 0.25
    )
    pen_plan = decoration_pen_plan(
        ink=ink,
        requested_width_mm=requested_nib_mm,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    nib_mm = pen_plan.pen.mark_width_mm
    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    weight = policy.weight("map_furniture")
    furniture_pitch_mm, furniture_width_mm = type_weight_plan(
        stroke_count=weight, nib_mm=nib_mm
    )
    placement = policy.placement("legend")
    furniture_cap_height = max(2.0, 8 * nib_mm)

    if layout.preset in A5_POSTER_PRESETS:
        preferred = (
            placement.cap_height_mm
            if placement is not None
            else float(layout_plate_format(layout)["type_scale_mm"]["legend"])
        )
        furniture_cap_height = max(preferred, 8 * nib_mm)
    bar_x, bar_y, label_y = scale_block_geometry(
        layout, placement, cap_height_mm=furniture_cap_height
    )

    furniture_label = "Map scale and north mark" if include_scale_bar else "North mark"
    group_label = f"93 — {furniture_label} — {pen_plan.pen.label}"
    color = policy.preview_color("map_furniture")
    group = _group(
        root,
        group_id="layer-map_furniture",
        label=group_label,
        color=color,
        plan=pen_plan,
        ink=ink,
        profile_id=profile_id,
        stroke_count=weight,
        offset_pitch_mm=furniture_pitch_mm,
        plotted_width_mm=furniture_width_mm,
    )
    bar: list[tuple[float, float]] = []
    label_strokes: list[Stroke] = []
    furniture_copy = ["N"] if draw_north else []
    path_count = 0
    if include_scale_bar:
        target_bar_mm = min(24.0, layout.map_width_mm * 0.22)
        distance_m = nice_scale_distance(target_bar_mm / layout.scale_mm_per_m)
        bar_mm = distance_m * layout.scale_mm_per_m
        bar = [
            (bar_x, bar_y - 1.0),
            (bar_x, bar_y),
            (bar_x + bar_mm, bar_y),
            (bar_x + bar_mm, bar_y - 1.0),
        ]
        ET.SubElement(group, svg_tag("path"), {"d": path_data(bar)})
        distance_label = scale_distance_label(distance_m)
        furniture_copy.append(distance_label)
        label_strokes = reliable_vector_strokes(
            weighted(
                stroke_text(
                    distance_label,
                    x_mm=bar_x,
                    y_mm=label_y,
                    height_mm=furniture_cap_height,
                ),
                placement_or_count=weight,
                nib_mm=nib_mm,
            ),
            nib_mm=nib_mm,
        )
        path_count = 1 + append_vector_strokes(
            group,
            label_strokes,
            {"data-scale-distance-m": format_number(distance_m)},
        )

    north_label: list[Stroke] = []
    if draw_north:
        north, _, _ = north_mark_geometry(layout, placement=policy.north_mark)
        ET.SubElement(group, svg_tag("path"), {"d": path_data(north)})
        path_count += 1
        north_label = reliable_vector_strokes(
            weighted(
                north_label_strokes(
                    layout,
                    cap_height_mm=furniture_cap_height,
                    placement=policy.north_mark,
                ),
                placement_or_count=weight,
                nib_mm=nib_mm,
            ),
            nib_mm=nib_mm,
        )
        path_count += append_vector_strokes(group, north_label, {"data-north": "true"})
    else:
        north = []
    group.set("data-copy", " / ".join(furniture_copy))
    group.set("data-line-count", "1")
    group.set("data-cap-height-mm", format_measurement(furniture_cap_height))
    group.set(
        "data-copy-geometry-sha256",
        stroke_geometry_sha256([*label_strokes, *north_label]),
    )
    length_mm = (
        polyline_length(bar)
        + polyline_length(north)
        + _total_length(label_strokes)
        + _total_length(north_label)
    )
    _record(
        layer_stats,
        layer_id="map_furniture",
        label=furniture_label,
        plan=pen_plan,
        ink=ink,
        color=color,
        profile_id=profile_id,
        path_count=path_count,
        length_mm=length_mm,
        group_id="layer-map_furniture",
        group_label=group_label,
        stroke_count=weight,
        offset_pitch_mm=furniture_pitch_mm,
        plotted_width_mm=furniture_width_mm,
    )


def _attribution_block(layout: Layout, placement: TextPlacement) -> list[Stroke]:
    """Two-part attribution, set inside whichever zone the theme gave it.

    Both the emitter and the contract verifier call this, so an attribution
    line cannot be drawn in one place and checked against another.
    """

    zone = _zone(layout, placement.zone, role=placement.role)
    usable = usable_zone(zone, placement)
    cap = placement.cap_height_mm
    left_width = text_width_mm(ATTRIBUTION_TEXT, cap_height_mm=cap)
    right_width = text_width_mm(ATTRIBUTION_URL, cap_height_mm=cap)
    if left_width + right_width > usable.width_mm + 1e-9:
        raise MapPlotterError(
            "The two-part OpenStreetMap attribution needs "
            f"{left_width + right_width:.3g} mm at its {cap:g} mm cap height, but "
            f"zone {placement.zone!r} offers {usable.width_mm:.3g} mm. Use a wider "
            "zone or a bigger sheet."
        )
    y_mm = _block_y(usable, placement, cap)
    right_x = usable.x_mm + usable.width_mm - right_width
    if placement.align == "split":
        positions = [usable.x_mm, right_x]
    elif placement.align == "right":
        positions = [right_x - left_width - cap, right_x]
    elif placement.align == "centre":
        block = left_width + right_width + cap
        first = usable.x_mm + (usable.width_mm - block) / 2
        positions = [first, first + left_width + cap]
    else:
        positions = [usable.x_mm, usable.x_mm + left_width + cap]
    return [
        *stroke_text(ATTRIBUTION_TEXT, x_mm=positions[0], y_mm=y_mm, height_mm=cap),
        *stroke_text(ATTRIBUTION_URL, x_mm=positions[1], y_mm=y_mm, height_mm=cap),
    ]


def _plain_attribution_block(layout: Layout) -> tuple[list[Stroke], float, float]:
    """The non-poster footer: two stacked lines under the map."""

    available_width = layout.page.width_mm - 2 * layout.margin_mm
    widest_units = max(
        text_width_units(ATTRIBUTION_TEXT), text_width_units(ATTRIBUTION_URL)
    )
    cap = min(2.0, available_width * 6 / widest_units)
    if cap < 1.4:
        raise MapPlotterError(
            "The printable page width is too narrow for readable OpenStreetMap "
            "attribution. Use wider paper or smaller margins."
        )
    line_gap = 0.45
    block_height = 2 * cap + line_gap
    top = (
        layout.page.height_mm - layout.margin_mm - (layout.footer_mm + block_height) / 2
    )
    return (
        [
            *stroke_text(
                ATTRIBUTION_TEXT, x_mm=layout.margin_mm, y_mm=top, height_mm=cap
            ),
            *stroke_text(
                ATTRIBUTION_URL,
                x_mm=layout.margin_mm,
                y_mm=top + cap + line_gap,
                height_mm=cap,
            ),
        ],
        cap,
        0.25,
    )


def append_attribution(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    policy: FurniturePolicy | None = None,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> None:
    poster = layout.preset in A5_POSTER_PRESETS
    if poster and policy is None:
        policy = default_furniture_policy(layout)
    if policy is not None and not policy.draws("attribution"):
        return

    if poster:
        assert policy is not None
        placement = policy.placement("attribution")
        if placement is None:
            raise MapPlotterError("This theme gives attribution no zone.")
        ink = policy.ink("attribution")
        colour = policy.preview_color("attribution")
        weight = policy.weight("attribution")
        requested_width = float(
            layout_plate_format(layout)["nib_roles_mm"][
                policy.nib_role("attribution", "hairline")
            ]
        )
        base_strokes = _attribution_block(layout, placement)
        cap_height = placement.cap_height_mm
        line_split = 1
    else:
        ink = "Black"
        colour = "#18181b"
        weight = 1
        base_strokes, cap_height, requested_width = _plain_attribution_block(layout)
        line_split = 1

    pen_plan = decoration_pen_plan(
        ink=ink,
        requested_width_mm=requested_width,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    stroke_width = pen_plan.pen.mark_width_mm
    pen = pen_plan.pen.label
    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    minimum_cap_height = 8 * stroke_width
    if cap_height + 1e-9 < minimum_cap_height:
        raise MapPlotterError(
            f"The attribution is set at {cap_height:.3g} mm, below the physical "
            f"{minimum_cap_height:g} mm cap-height floor for {pen}. Raise its "
            "cap_scale or give it a finer pen."
        )
    pitch_mm, plotted_width_mm = type_weight_plan(
        stroke_count=weight, nib_mm=stroke_width
    )
    drawn = reliable_vector_strokes(
        weighted(base_strokes, placement_or_count=weight, nib_mm=stroke_width),
        nib_mm=stroke_width,
    )
    group_label = f"99 — Attribution — {pen}"
    attribution = _group(
        root,
        group_id="layer-attribution",
        label=group_label,
        color=colour,
        plan=pen_plan,
        ink=ink,
        profile_id=profile_id,
        stroke_count=weight,
        offset_pitch_mm=pitch_mm,
        plotted_width_mm=plotted_width_mm,
        extra={
            "data-copy": f"{ATTRIBUTION_TEXT} / {ATTRIBUTION_URL}",
            "data-line-count": str(line_split),
            "data-cap-height-mm": format_measurement(cap_height),
            "data-copy-geometry-sha256": stroke_geometry_sha256(drawn),
        },
    )
    ET.SubElement(
        attribution, svg_tag("title")
    ).text = "Map data © OpenStreetMap contributors — https://www.openstreetmap.org/copyright"
    path_count = append_vector_strokes(attribution, drawn)
    _record(
        layer_stats,
        layer_id="attribution",
        label="OpenStreetMap attribution",
        plan=pen_plan,
        ink=ink,
        color=colour,
        profile_id=profile_id,
        path_count=path_count,
        length_mm=_total_length(drawn),
        group_id="layer-attribution",
        group_label=group_label,
        stroke_count=weight,
        offset_pitch_mm=pitch_mm,
        plotted_width_mm=plotted_width_mm,
    )


def append_poster_decoration(
    root: ET.Element,
    layout: Layout,
    *,
    title: str,
    subtitle: str | None,
    detail_lines: tuple[str, ...],
    poster_layout: str,
    person_name: str | None,
    degree: str | None,
    honours: str | None,
    years: str | None,
    memorabilia_variant: str,
    layer_stats: list[dict[str, Any]],
    rowing_course: Any | None = None,
    policy: FurniturePolicy | None = None,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> None:
    """Draw the border and every piece of poster copy, zone by zone."""

    policy = policy or default_furniture_policy(layout)
    poster_format = layout_plate_format(layout)
    nib_roles = poster_format["nib_roles_mm"]
    profile_id = pen_inventory.id if pen_inventory is not None else "style"

    if policy.draws("poster_border") and policy.border_style != "none":
        border_ink = policy.ink("poster_border")
        border_nib_role = policy.nib_role(
            "poster_border",
            "hairline" if policy.border_style == "hairline" else "heavy",
        )
        border_pen = decoration_pen_plan(
            ink=border_ink,
            requested_width_mm=float(nib_roles[border_nib_role]),
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        border_zone = layout.zones["outer_border"]
        border_color = policy.preview_color("poster_border")
        border_label = (
            f"95 — {poster_format['sheet']} safe border — {border_pen.pen.label}"
        )
        border = _group(
            root,
            group_id="layer-poster_border",
            label=border_label,
            color=border_color,
            plan=border_pen,
            ink=border_ink,
            profile_id=profile_id,
            linecap="butt",
            linejoin="miter",
        )
        paths = border_geometry(
            border_zone,
            style=policy.border_style,
            inner_offset_mm=float(poster_format["border"]["inner_offset_mm"]),
        )
        for points in paths:
            ET.SubElement(border, svg_tag("path"), {"d": path_data(points)})
        _record(
            layer_stats,
            layer_id="poster_border",
            label=f"{poster_format['sheet']} safe border",
            plan=border_pen,
            ink=border_ink,
            color=border_color,
            profile_id=profile_id,
            path_count=len(paths),
            length_mm=_total_length(paths),
            group_id="layer-poster_border",
            group_label=border_label,
        )

    if poster_layout == "rowing-course":
        if rowing_course is None:
            raise MapPlotterError("The rowing-course layout needs a course.")
        append_rowing_course_copy(
            root,
            layout,
            course=rowing_course,
            layer_stats=layer_stats,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        return

    if poster_layout == "university-memorabilia":
        append_university_memorabilia_copy(
            root,
            layout,
            title=title,
            person_name=person_name,
            degree=degree,
            honours=honours,
            years=years,
            memorabilia_variant=memorabilia_variant,
            layer_stats=layer_stats,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        return

    if poster_layout == "city-map":
        append_city_map_copy(
            root,
            layout,
            title=title,
            layer_stats=layer_stats,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        return

    _append_role(
        root,
        layout,
        layer_stats,
        role="title",
        layer_id="poster_title",
        label="Poster title",
        layer_number=96,
        copy=[title],
        policy=policy,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    if subtitle:
        _append_role(
            root,
            layout,
            layer_stats,
            role="subtitle",
            layer_id="poster_subtitle",
            label="Poster subtitle",
            layer_number=97,
            copy=[subtitle],
            policy=policy,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
    visible_details = list(detail_lines[:3]) or ["CITY MAP"]
    _append_role(
        root,
        layout,
        layer_stats,
        role="detail",
        layer_id="poster_details",
        label="City details",
        layer_number=98,
        copy=visible_details,
        policy=policy,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )


def _append_role(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    role: str,
    layer_id: str,
    label: str,
    layer_number: int,
    copy: list[str],
    policy: FurniturePolicy,
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> None:
    if not policy.draws(layer_id):
        return
    placement = policy.placement(role)
    if placement is None:
        raise MapPlotterError(f"This theme gives typography role {role!r} no zone.")
    plate = layout_plate_format(layout)
    ink = policy.ink(layer_id)
    nib_role = policy.nib_role(layer_id, str(plate["type_nib_role"][role]))
    plan = decoration_pen_plan(
        ink=ink,
        requested_width_mm=float(plate["nib_roles_mm"][nib_role]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    nib_mm = plan.pen.mark_width_mm
    weight = policy.weight(layer_id)
    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    color = policy.preview_color(layer_id)
    line_step = _detail_line_step(layout, placement) if role == "detail" else None
    block = set_text_block(
        copy, layout=layout, placement=placement, line_step_mm=line_step
    )
    pitch_mm, plotted_width_mm = type_weight_plan(stroke_count=weight, nib_mm=nib_mm)
    group_label = f"{layer_number} — {label} — {plan.pen.label}"
    group = _group(
        root,
        group_id=f"layer-{layer_id}",
        label=group_label,
        color=color,
        plan=plan,
        ink=ink,
        profile_id=profile_id,
        stroke_count=weight,
        offset_pitch_mm=pitch_mm,
        plotted_width_mm=plotted_width_mm,
    )
    group.set("data-cap-height-mm", format_measurement(block.cap_height_mm))
    emitted: list[Stroke] = []
    path_count = 0
    if role == "detail":
        group.set("data-line-count", str(block.line_count))
        for line_number, (line, line_cap, line_strokes) in enumerate(
            block.line_strokes, start=1
        ):
            drawn = reliable_vector_strokes(
                weighted(line_strokes, placement_or_count=weight, nib_mm=nib_mm),
                nib_mm=nib_mm,
            )
            emitted.extend(drawn)
            path_count += append_vector_strokes(
                group,
                drawn,
                {
                    "data-detail-line": str(line_number),
                    "data-copy": copy[line_number - 1],
                    "data-cap-height-mm": format_measurement(line_cap),
                },
            )
    else:
        group.set("data-copy", copy[0])
        if role == "title":
            group.set("data-title-lines", str(block.line_count))
        emitted = reliable_vector_strokes(
            weighted(block.strokes, placement_or_count=weight, nib_mm=nib_mm),
            nib_mm=nib_mm,
        )
        path_count = append_vector_strokes(group, emitted)
    group.set("data-copy-geometry-sha256", stroke_geometry_sha256(emitted))
    _record(
        layer_stats,
        layer_id=layer_id,
        label=label,
        plan=plan,
        ink=ink,
        color=color,
        profile_id=profile_id,
        path_count=path_count,
        length_mm=_total_length(emitted),
        group_id=f"layer-{layer_id}",
        group_label=group_label,
        stroke_count=weight,
        offset_pitch_mm=pitch_mm,
        plotted_width_mm=plotted_width_mm,
    )


def with_crew_zones(layout: Layout) -> Layout:
    """Switch a poster layout onto the crew composition.

    The crew stack gives up map height to the crew list, so this does two
    things: it publishes the extra bands, and it **re-fits the map into the
    shorter field**. Doing that here, before anything is projected, means
    the map, the clip, the manifest and the ink measurement all agree about
    where the map field is -- there is no second opinion later.
    """

    if layout.format_id is None:
        raise MapPlotterError("The crew composition needs a plate format.")
    plate = load_plate_format(layout.format_id)
    bands = plate.get("crew_zones_mm")
    if not bands:
        raise MapPlotterError(
            f"Plate format {layout.format_id!r} publishes no crew composition."
        )
    zones = {
        name: Rect(
            float(rect["x"]),
            float(rect["y"]),
            float(rect["width"]),
            float(rect["height"]),
        )
        for name, rect in bands.items()
    }
    field = zones["crew_map_field"]
    scale = min(
        field.width_mm / layout.bbox.approximate_width_m,
        field.height_mm / layout.bbox.approximate_height_m,
    )
    map_width = layout.bbox.approximate_width_m * scale
    map_height = layout.bbox.approximate_height_m * scale
    map_x = field.x_mm + (field.width_mm - map_width) / 2
    map_y = field.y_mm + (field.height_mm - map_height) / 2
    merged = {**layout.zones, **zones}
    merged["map_field"] = field
    merged["map"] = Rect(map_x, map_y, map_width, map_height)
    return replace(
        layout,
        zones=merged,
        map_x_mm=map_x,
        map_y_mm=map_y,
        map_width_mm=map_width,
        map_height_mm=map_height,
        scale_mm_per_m=scale,
    )


def _column_block(
    entries: Sequence[tuple[str, str]],
    *,
    x_mm: float,
    first_row_y: float,
    leading: float,
    cap_mm: float,
) -> list[Stroke]:
    """A two-part list: labels on one vertical, values on the next.

    The label column is as wide as the widest label, so every value starts on
    the same vertical and the block reads as one list rather than as pairs.
    """

    if not entries:
        return []
    label_column = max(
        text_width_mm(label, cap_height_mm=cap_mm) for label, _ in entries
    )
    value_x = x_mm + label_column + cap_mm * 1.1
    strokes: list[Stroke] = []
    for index, (label, value) in enumerate(entries):
        y = first_row_y + index * leading
        strokes.extend(stroke_text(label, x_mm=x_mm, y_mm=y, height_mm=cap_mm))
        strokes.extend(stroke_text(value, x_mm=value_x, y_mm=y, height_mm=cap_mm))
    return strokes


def append_crew_head(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    title: str,
    meta_lines: Sequence[str],
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> None:
    """Race name, then two lines of context, then a compass on its own.

    Three text lines on one left edge, and the compass lifted out of the stack
    into its own column so nothing has to dodge it.
    """

    title_zone = _zone(layout, "crew_title", role="crew_title")
    meta_zone = _zone(layout, "crew_meta", role="crew_meta")
    compass_zone = _zone(layout, "crew_compass", role="crew_compass")
    plate = layout_plate_format(layout)
    type_scale = plate["type_scale_mm"]
    display_pen = decoration_pen_plan(
        ink="Black",
        requested_width_mm=float(plate["nib_roles_mm"]["primary"]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    text_pen = decoration_pen_plan(
        ink="Black",
        requested_width_mm=float(plate["nib_roles_mm"]["text"]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    profile_id = pen_inventory.id if pen_inventory is not None else "style"

    title_copy = normalise_text(title).upper()
    preferred = float(type_scale["title"])
    natural = display_text_width_mm(title_copy, height_mm=preferred)
    title_height = min(preferred, preferred * title_zone.width_mm / max(natural, 1e-9))
    minimum_title = 8 * display_pen.pen.mark_width_mm
    if title_height + 1e-9 < minimum_title:
        raise MapPlotterError(
            f"Race title {title!r} cannot fit the head at the physical "
            f"{minimum_title:g} mm cap-height floor. Shorten it."
        )
    title_strokes = reliable_vector_strokes(
        display_text(
            title_copy,
            x_mm=title_zone.x_mm,
            y_mm=title_zone.y_mm + (title_zone.height_mm - title_height) / 2,
            height_mm=title_height,
            anchor="start",
        ),
        nib_mm=display_pen.pen.mark_width_mm,
    )
    title_font = display_font_contract()
    title_label = f"96 — Race title — {display_pen.pen.label}"
    title_group = _group(
        root,
        group_id="layer-poster_title",
        label=title_label,
        color="#26333d",
        plan=display_pen,
        ink="Black",
        profile_id=profile_id,
        extra={
            "data-copy": title,
            "data-cap-height-mm": format_measurement(title_height),
            "data-stroke-font-id": str(title_font["font_id"]),
            "data-stroke-font-sha256": str(title_font["sha256"]),
            "data-copy-geometry-sha256": stroke_geometry_sha256(title_strokes),
        },
    )
    _record(
        layer_stats,
        layer_id="poster_title",
        label="Race title",
        plan=display_pen,
        ink="Black",
        color="#26333d",
        profile_id=profile_id,
        path_count=append_vector_strokes(title_group, title_strokes),
        length_mm=_total_length(title_strokes),
        group_id="layer-poster_title",
        group_label=title_label,
    )

    lines = [line for line in meta_lines if line]
    if lines:
        cap = float(type_scale["detail"])
        for line in lines:
            natural = text_width_mm(line, cap_height_mm=cap)
            if natural > meta_zone.width_mm:
                cap = min(cap, cap * meta_zone.width_mm / natural)
        floor_mm = 8 * text_pen.pen.mark_width_mm
        if cap + 1e-9 < floor_mm:
            raise MapPlotterError(
                f"The head lines only fit at {cap:.3g} mm, below the physical "
                f"{floor_mm:g} mm cap-height floor. Shorten them."
            )
        leading = (
            (meta_zone.height_mm - cap) / (len(lines) - 1) if len(lines) > 1 else 0.0
        )
        meta_strokes: list[Stroke] = []
        for index, line in enumerate(lines):
            meta_strokes.extend(
                stroke_text(
                    line,
                    x_mm=meta_zone.x_mm,
                    y_mm=meta_zone.y_mm + index * leading,
                    height_mm=cap,
                )
            )
        meta_strokes = reliable_vector_strokes(
            meta_strokes, nib_mm=text_pen.pen.mark_width_mm
        )
        meta_label = f"97 — Head lines — {text_pen.pen.label}"
        meta_group = _group(
            root,
            group_id="layer-poster_subtitle",
            label=meta_label,
            color="#58636d",
            plan=text_pen,
            ink="Black",
            profile_id=profile_id,
            extra={
                "data-copy": " | ".join(lines),
                "data-line-count": str(len(lines)),
                "data-cap-height-mm": format_measurement(cap),
                "data-copy-geometry-sha256": stroke_geometry_sha256(meta_strokes),
            },
        )
        _record(
            layer_stats,
            layer_id="poster_subtitle",
            label="Head lines",
            plan=text_pen,
            ink="Black",
            color="#58636d",
            profile_id=profile_id,
            path_count=append_vector_strokes(meta_group, meta_strokes),
            length_mm=_total_length(meta_strokes),
            group_id="layer-poster_subtitle",
            group_label=meta_label,
        )

    # The compass has a column to itself, so it is drawn to the column rather
    # than squeezed under a line of coordinates.
    compass_cap = float(type_scale["subtitle"])
    compass_x = compass_zone.x_mm + compass_zone.width_mm / 2
    compass_top = compass_zone.y_mm + compass_cap + 1.2
    compass_bottom = compass_zone.y_mm + compass_zone.height_mm
    compass_middle = (compass_top + compass_bottom) / 2
    half = compass_zone.width_mm * 0.30
    compass_strokes = reliable_vector_strokes(
        [
            *stroke_text(
                "N",
                x_mm=compass_x,
                y_mm=compass_zone.y_mm,
                height_mm=compass_cap,
                anchor="middle",
            ),
            [
                (compass_x, compass_top),
                (compass_x + half, compass_middle),
                (compass_x, compass_bottom),
                (compass_x - half, compass_middle),
                (compass_x, compass_top),
            ],
            [(compass_x, compass_top), (compass_x, compass_bottom)],
            [(compass_x - half, compass_middle), (compass_x + half, compass_middle)],
        ],
        nib_mm=display_pen.pen.mark_width_mm,
    )
    compass_label = f"93 — Compass — {display_pen.pen.label}"
    compass_group = _group(
        root,
        group_id="layer-poster_compass",
        label=compass_label,
        color="#26333d",
        plan=display_pen,
        ink="Black",
        profile_id=profile_id,
        extra={"data-copy": "N"},
    )
    _record(
        layer_stats,
        layer_id="poster_compass",
        label="Compass",
        plan=display_pen,
        ink="Black",
        color="#26333d",
        profile_id=profile_id,
        path_count=append_vector_strokes(compass_group, compass_strokes),
        length_mm=_total_length(compass_strokes),
        group_id="layer-poster_compass",
        group_label=compass_label,
    )


def append_crew_block(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    crew: Any,
    course: Any | None = None,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> dict[str, Any]:
    """Crew down the left, result down the right, on one shared grid.

    Both halves sit under standing heads on the same rule and share the same
    baselines, so the block reads as one table and everything on the sheet --
    title, subtitle, map edge, both headings -- lines up on the plate's own
    left margin.
    """

    from .crew import crew_list_rows

    zone = _zone(layout, "crew_block", role="crew_block")
    plate = layout_plate_format(layout)
    type_scale = plate["type_scale_mm"]
    plan = decoration_pen_plan(
        ink="Black",
        requested_width_mm=float(plate["nib_roles_mm"]["text"]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    nib_mm = plan.pen.mark_width_mm
    floor_mm = 8 * nib_mm
    head_cap = float(type_scale["detail"])
    cap = float(type_scale["detail"]) * CREW_BLOCK_BODY

    crew_entries = crew_list_rows(crew)
    result_entries = list(crew.result)
    if not result_entries and course is not None:
        result_entries = [tuple(pair) for pair in course.poster["fields"]]

    # The crew takes the wider half; the result is labels and short values.
    gutter_mm = nib_mm * 20
    crew_width = (zone.width_mm - gutter_mm) * 0.56
    result_width = zone.width_mm - gutter_mm - crew_width
    result_x = zone.x_mm + crew_width + gutter_mm

    for entries, available in (
        (crew_entries, crew_width),
        (result_entries, result_width),
    ):
        for label, value in entries:
            natural = text_width_mm(f"{label}  {value}", cap_height_mm=cap)
            if natural > available:
                cap = min(cap, cap * available / natural)
    if cap + 1e-9 < floor_mm:
        raise MapPlotterError(
            f"The crew block only fits at {cap:.3g} mm, below the physical "
            f"{floor_mm:g} mm cap-height floor. Shorten the longest name or "
            "result value, or use a larger sheet."
        )

    rule_y = zone.y_mm + head_cap + head_cap * 0.55
    first_row_y = rule_y + head_cap * 0.95
    rows = max(len(crew_entries), len(result_entries))
    if rows > CREW_BLOCK_ROWS:
        raise MapPlotterError(
            f"The crew block holds {CREW_BLOCK_ROWS} rows and was given {rows}. "
            "An eight and cox is the largest crew the band is sized for."
        )
    # Leading is set by the band's capacity, not by this crew, so an eight and a
    # four are set on the same rhythm and a small crew leaves the tail of the
    # band empty instead of being stretched across it.
    remaining = zone.y_mm + zone.height_mm - first_row_y
    leading = (remaining - cap) / (CREW_BLOCK_ROWS - 1)
    if leading + 1e-9 < cap * 1.15:
        raise MapPlotterError(
            f"The crew block allows only {leading:.3g} mm of leading for a "
            f"{cap:.3g} mm line."
        )

    crew_heading = crew.boat.label.upper()
    if crew.club:
        crew_heading = f"{crew_heading} - {crew.club.upper()}"
    strokes: list[Stroke] = list(
        stroke_text(crew_heading, x_mm=zone.x_mm, y_mm=zone.y_mm, height_mm=head_cap)
    )
    if result_entries:
        strokes.extend(
            stroke_text("RESULT", x_mm=result_x, y_mm=zone.y_mm, height_mm=head_cap)
        )
    for entries, left in ((crew_entries, zone.x_mm), (result_entries, result_x)):
        strokes.extend(
            _column_block(
                entries,
                x_mm=left,
                first_row_y=first_row_y,
                leading=leading,
                cap_mm=cap,
            )
        )
    strokes = reliable_vector_strokes(strokes, nib_mm=nib_mm)
    rule = [(zone.x_mm, rule_y), (zone.x_mm + zone.width_mm, rule_y)]

    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    group_label = f"91 — Crew and result — {plan.pen.label}"
    group = _group(
        root,
        group_id="layer-crew_block",
        label=group_label,
        color="#26333d",
        plan=plan,
        ink="Black",
        profile_id=profile_id,
        extra={
            "data-crew-json": json.dumps(
                {
                    "boat_class": str(crew.boat.id),
                    "crew": [
                        {"seat": label, "name": name} for label, name in crew_entries
                    ],
                    "result": [
                        {"label": label, "value": value}
                        for label, value in result_entries
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "data-cap-height-mm": format_measurement(cap),
            "data-copy-geometry-sha256": stroke_geometry_sha256(strokes),
        },
    )
    ET.SubElement(group, svg_tag("path"), {"d": path_data(rule)})
    path_count = 1 + append_vector_strokes(group, strokes)
    _record(
        layer_stats,
        layer_id="crew_block",
        label="Crew and result",
        plan=plan,
        ink="Black",
        color="#26333d",
        profile_id=profile_id,
        path_count=path_count,
        length_mm=_total_length(strokes) + polyline_length(rule),
        group_id="layer-crew_block",
        group_label=group_label,
    )
    return {
        "crew_rows": len(crew_entries),
        "result_rows": len(result_entries),
        "cap_height_mm": round(cap, 4),
        "heading": crew_heading,
        "paths": path_count,
    }


def append_rowing_crew_copy(
    root: ET.Element,
    layout: Layout,
    *,
    crew: Any,
    course: Any | None,
    layer_stats: list[dict[str, Any]],
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> dict[str, Any]:
    """The whole crew composition: head, crew block, and the result beside it."""

    # The head band is narrow, so the title has to be the short display name. A
    # crew file may set its own; otherwise the course's display title wins over
    # the long event name.
    title = (
        crew.title
        or (course.title if course is not None else "")
        or crew.event
        or crew.club
    )
    place_parts: list[str] = []
    if course is not None:
        place_parts.extend(part for part in (course.city, course.river) if part)
    place_parts.append(coordinate_label(layout))
    meta_lines = [crew.meta_line(), " / ".join(place_parts).upper()]
    append_crew_head(
        root,
        layout,
        layer_stats,
        title=title,
        meta_lines=meta_lines,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    listing = append_crew_block(
        root,
        layout,
        layer_stats,
        crew=crew,
        course=course,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    return {**crew.as_dict(), "head_lines": meta_lines, "listing": listing}


def course_label_layout(
    course: Any, layout: Layout, *, cap_mm: float, nib_mm: float
) -> list[tuple[Any, tuple[float, float], list[Stroke]]]:
    """Place every course marker on the page, dropping any that will not fit.

    A landmark or distance mark is set beside the point it names, offset
    perpendicular to the course so the label sits off the water. Bank names are
    pushed further out to their own side. Anything whose label would leave the
    map field is dropped rather than clipped -- a half-printed place name is
    worse than no place name.
    """

    from shapely.geometry import LineString as _Line
    from shapely.geometry import box as _box
    from shapely.ops import unary_union as _union

    from .rowing import end_bar as _end_bar
    from .rowing import project_course as _project_course
    from .styles import race_course_target_mm as _race_course_target_mm

    placed: list[tuple[Any, tuple[float, float], list[Stroke]]] = []
    boxes: list[tuple[float, float, float, float]] = []
    if not getattr(course, "markers", ()):  # pragma: no cover - no markers
        return placed
    page = [layout.project_to_page(lat, lon) for lat, lon in course.waypoints]
    line = _Line(page)
    # The course on the page is not its mathematical centreline: it is drawn
    # to the plate's course width in parallel passes, with a bar square across
    # the water at each end. A label must clear that ink -- an end bar's tip
    # grazing a label halo is still ink on the label's paper -- so the
    # keep-out is the drawn geometry at its half-width, not the bare line.
    course_ink: list[Any] = [line]
    projected_parts = _project_course(course, layout)
    for at_start in (True, False):
        bar = _end_bar(projected_parts, layout, at_start=at_start)
        if bar is not None:
            course_ink.append(_Line(bar))
    course_half_width_mm = (
        _race_course_target_mm(layout.format_id) / 2.0
        if layout.format_id is not None
        else 0.0
    )
    keep_out = _union(course_ink)
    if course_half_width_mm > 0.0:
        keep_out = keep_out.buffer(course_half_width_mm)
    left, top, right, bottom = layout.clip_rect
    margin = cap_mm * 0.5
    # When two labels want the same paper, the one that tells a rower more
    # wins: the station they are on, then the bridge they are passing, then
    # where they are, then whose boathouse that is. Distance marks go last --
    # there are four of them and they repeat.
    priority = {
        "bank": 0,
        "bridge": 1,
        "place": 2,
        "club": 3,
        "landmark": 4,
        "distance_mark": 5,
    }
    for marker in sorted(
        course.markers, key=lambda m: (priority.get(m.kind, 3), m.along_m)
    ):
        anchor_point = layout.project_to_page(marker.lat, marker.lon)
        along = line.project(_Line([anchor_point, anchor_point]).centroid)
        step = min(max(along, 0.5), line.length - 0.5)
        before = line.interpolate(max(0.0, step - 1.0))
        after = line.interpolate(min(line.length, step + 1.0))
        dx, dy = after.x - before.x, after.y - before.y
        length = (dx * dx + dy * dy) ** 0.5 or 1.0
        # Normal to the course, left of travel.
        normal = (dy / length, -dx / length)
        # Far enough off the line that the label clears the course itself, and
        # station names further still so they read as the banks, not the water.
        # Reach has to clear the label's own height plus the course, or the
        # keep-out below rejects the placement and the label is lost. Tried
        # outward first, then further out again.
        reaches = (
            [cap_mm * 8.0, cap_mm * 11.0]
            if marker.kind == "bank"
            else [cap_mm * 4.0, cap_mm * 6.5, cap_mm * 9.0]
        )
        width = text_width_mm(marker.label, cap_height_mm=cap_mm)
        # A bank name belongs on its own side. Anything else takes whichever
        # side is clear: on a bend the outside of the curve is often the only
        # place a label does not land back on the water.
        chosen: tuple[float, float, tuple[float, float, float, float]] | None = None
        for reach, side in (
            (reach, side)
            for reach in reaches
            for side in ([marker.side] if marker.side else [1, -1])
        ):
            centre = (
                anchor_point[0] + normal[0] * side * reach,
                anchor_point[1] + normal[1] * side * reach,
            )
            x = centre[0] - width / 2
            y = centre[1] - cap_mm / 2
            if (
                x < left + margin
                or x + width > right - margin
                or y < top + margin
                or y + cap_mm > bottom - margin
            ):
                continue
            box = (x - margin, y - margin, x + width + margin, y + cap_mm + margin)
            # Two labels cannot share the paper, and neither can a label and the
            # course: the course is the subject of the plate.
            if any(
                box[0] < other[2]
                and other[0] < box[2]
                and box[1] < other[3]
                and other[1] < box[3]
                for other in boxes
            ) or keep_out.intersects(_box(*box)):
                continue
            chosen = (x, y, box)
            break
        if chosen is None:
            continue
        x, y, box = chosen
        boxes.append(box)
        strokes = stroke_text(marker.label, x_mm=x, y_mm=y, height_mm=cap_mm)
        placed.append((marker, (x, y), reliable_vector_strokes(strokes, nib_mm=nib_mm)))
    placed.sort(key=lambda item: (item[0].along_m, item[0].label))
    return placed


def label_knockouts(
    placed: Sequence[tuple[Any, tuple[float, float], list[Stroke]]],
    *,
    cap_mm: float,
    halo_mm: float,
) -> list[tuple[float, float, float, float]]:
    """The rectangles map linework must keep out of, one per placed label."""

    boxes: list[tuple[float, float, float, float]] = []
    for _, (x, y), strokes in placed:
        xs = [point[0] for stroke in strokes for point in stroke]
        ys = [point[1] for stroke in strokes for point in stroke]
        if not xs:
            continue
        boxes.append(
            (
                min(xs) - halo_mm,
                min(ys) - halo_mm,
                max(xs) + halo_mm,
                max(ys) + halo_mm,
            )
        )
    return boxes


@dataclass(frozen=True)
class CourseLabelPlan:
    """Where the course labels go, and what the map must keep out of."""

    placed: list[tuple[Any, tuple[float, float], list[Stroke]]]
    knockouts: list[tuple[float, float, float, float]]
    cap_mm: float
    nib_mm: float
    dropped: int


def plan_course_labels(
    layout: Layout,
    *,
    course: Any,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> CourseLabelPlan:
    """Lay the labels out once, so the knockout and the ink agree exactly."""

    plate = layout_plate_format(layout)
    plan = decoration_pen_plan(
        ink="Black",
        requested_width_mm=float(plate["nib_roles_mm"]["text"]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    nib_mm = plan.pen.mark_width_mm
    cap_mm = max(float(plate["type_scale_mm"]["attribution"]), 8 * nib_mm)
    placed = course_label_layout(course, layout, cap_mm=cap_mm, nib_mm=nib_mm)
    return CourseLabelPlan(
        placed=placed,
        knockouts=label_knockouts(placed, cap_mm=cap_mm, halo_mm=cap_mm * 0.42),
        cap_mm=cap_mm,
        nib_mm=nib_mm,
        dropped=len(getattr(course, "markers", ())) - len(placed),
    )


def knock_out_labels(
    strokes: Sequence[Any],
    knockouts: Sequence[tuple[float, float, float, float]],
    *,
    minimum_length_mm: float,
    close_mm: float = 0.0,
) -> tuple[list[Any], dict[str, Any]]:
    """Cut label-sized holes in the map linework.

    A plotter cannot draw a white halo on white paper: the only way to stop a
    place name colliding with the streets under it is to not draw those streets
    where the name goes. So the halo is a knockout, applied here to the compiled
    strokes -- before the manifest, the path counts and the ink are measured, so
    every one of them describes what is actually drawn.

    Fragments left under the physical floor are dropped and counted, the same
    gate every other branch applies. Label lettering owns its paper absolutely:
    a stroke the halo consumes is removed even when it was its source way's
    last drawn representation. That removal is never silent -- each one is
    recorded in the ``fully_removed`` ledger with a per-entry
    ``last_source_representation`` flag, which the highway completeness audit
    verifies and accepts as the ``course_label_knockout`` omission reason
    instead of reading the absence as missing geometry.
    """

    from dataclasses import replace as _replace

    from shapely.geometry import LineString as _Line
    from shapely.geometry import box as _box
    from shapely.ops import unary_union as _union

    if not knockouts:
        return list(strokes), {"applied": False}
    mask = _union([_box(*rectangle) for rectangle in knockouts])
    # Two labels stacked a sliver apart would each get a clean box with a
    # thread of streets left running between the lines. Morphological closing
    # -- dilate then erode by the same radius -- fills any gap narrower than
    # twice ``close_mm`` so near-touching boxes share one continuous piece of
    # blank paper, while every outer edge stays exactly where it was.
    if close_mm > 0.0:
        # Mitred, not round: for axis-aligned rectangles a mitred dilation
        # then erosion is exact, while round joins approximate each corner
        # with chords and can shave microns of coverage back off the box.
        mask = mask.buffer(close_mm, join_style="mitre").buffer(
            -close_mm, join_style="mitre"
        )

    # Pass one: cut everything, and note which source ways still have geometry
    # somewhere. A way that survives elsewhere does not need its consumed
    # fragment kept, which is what lets a label box come out genuinely clear
    # instead of with a stub of road drawn across it.
    plans: list[tuple[Any, list[Any], float]] = []
    dropped = 0
    for stroke in strokes:
        if len(stroke.points) < 2:
            plans.append((stroke, [stroke], 0.0))
            continue
        line = _Line(stroke.points)
        if not line.intersects(mask):
            plans.append((stroke, [stroke], 0.0))
            continue
        # A fragment that clears the floor for a 0.25 pen is still a dot under a
        # 0.4 one, so the nib comes from the stroke, not from the label.
        # Each stroke is gated on its OWN nib, with a small margin: the gate
        # measures full-precision geometry while the validator measures the
        # 0.001 mm serialised path data, and a fragment sitting exactly on the
        # floor can round under it.
        floor_mm = 1.02 * max(
            minimum_length_mm,
            3 * float(stroke.tags.get("plot:nib-mm", minimum_length_mm / 3)),
        )
        # The plotted mark is wider than its centre-line: a stroke cut exactly
        # on the halo rectangle still pushes half a nib of round pen cap into
        # the blank paper. Cutting against the mask dilated by that stroke's
        # own half-nib keeps the ink, not just the geometry, out of the box.
        nib_mm = float(stroke.tags.get("plot:nib-mm", minimum_length_mm / 3))
        remainder = line.difference(mask.buffer(nib_mm / 2.0))
        parts = (
            [remainder]
            if isinstance(remainder, _Line)
            else list(getattr(remainder, "geoms", ()))
        )
        survivors: list[Any] = []
        for index, part in enumerate(parts):
            if not isinstance(part, _Line) or len(part.coords) < 2:
                continue
            if part.length + 1e-9 < floor_mm:
                dropped += 1
                continue
            survivors.append(
                _replace(
                    stroke,
                    points=[(float(x), float(y)) for x, y in part.coords],
                    part=f"{stroke.part}.k{index}",
                    smooth=False,
                    tags={**stroke.tags, "plot:label-knockout": "true"},
                )
            )
        plans.append((stroke, survivors, max(0.0, line.length - remainder.length)))

    # Which (layer, source ref) pairs still have drawn geometry after the cut.
    # A way that keeps at least one drawn stroke elsewhere remains present for
    # the source-ref completeness audit, so its fragment under a label may be
    # removed outright -- that is what makes a label box genuinely blank
    # instead of stippled with water dots or crossed by a road stub.
    surviving_refs: set[tuple[str, str]] = set()
    for stroke, survivors, _removed in plans:
        if not survivors:
            continue
        for source_ref in stroke.tags.get("source-refs", "").split(";"):
            if source_ref:
                surviving_refs.add((stroke.layer, source_ref))

    # Pass two: emit. Lettering owns its paper absolutely: every stroke the
    # halo consumes is removed, following the transit and golf label-masking
    # precedent -- counted in the ledger below, never hidden. When that removal
    # takes a source way's LAST drawn representation, the way is not silently
    # gone: the ledger flags it, so the manifest records the design decision
    # the completeness audit would otherwise read as missing geometry.
    kept: list[Any] = []
    trimmed = 0
    removed_mm = 0.0
    fully_removed: list[dict[str, Any]] = []
    hidden_ways = 0
    for stroke, survivors, removed in plans:
        if survivors:
            if survivors != [stroke]:
                trimmed += 1
                removed_mm += removed
            kept.extend(survivors)
            continue
        refs = [
            source_ref
            for source_ref in stroke.tags.get("source-refs", "").split(";")
            if source_ref
        ]
        last_representation = bool(refs) and not all(
            (stroke.layer, source_ref) in surviving_refs for source_ref in refs
        )
        if last_representation:
            hidden_ways += 1
        length_mm = _Line(stroke.points).length if len(stroke.points) > 1 else 0.0
        removed_mm += length_mm
        fully_removed.append(
            {
                "part": stroke.part,
                "layer": stroke.layer,
                "source_refs": refs,
                "length_mm": round(length_mm, 3),
                "last_source_representation": last_representation,
                "reason": "fully-removed-by-course-label-knockout",
            }
        )
    return kept, {
        "applied": True,
        "regions": len(knockouts),
        "mask_closed_mm": round(close_mm, 4),
        "strokes_trimmed": trimmed,
        "strokes_preserved_whole": 0,
        "strokes_fully_removed": len(fully_removed),
        "source_ways_fully_hidden": hidden_ways,
        "fully_removed": fully_removed,
        "fragments_dropped_below_floor": dropped,
        "linework_removed_mm": round(removed_mm, 2),
        "policy": (
            "Label lettering owns its paper: every stroke the halo consumes "
            "is removed and recorded here, and each stroke is cut against the "
            "halo dilated by its own half-nib so no round pen cap reaches the "
            "blank box. A removal that hides a source way's last drawn "
            "representation is flagged per entry and counted, so the manifest "
            "records the design decision rather than leaving the completeness "
            "audit to read it as missing geometry."
        ),
    }


def append_course_labels(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    course: Any,
    label_plan: CourseLabelPlan | None = None,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> dict[str, Any]:
    """Name the bridges, the mile marks and the stations along the course."""

    if label_plan is None:
        label_plan = plan_course_labels(
            layout,
            course=course,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
    plate = layout_plate_format(layout)
    plan = decoration_pen_plan(
        ink="Black",
        requested_width_mm=float(plate["nib_roles_mm"]["text"]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    cap_mm = label_plan.cap_mm
    placed = label_plan.placed
    if not placed:
        return {"placed": 0, "dropped": label_plan.dropped}

    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    group_label = f"89 — Course labels — {plan.pen.label}"
    group = _group(
        root,
        group_id="layer-course_labels",
        label=group_label,
        color="#26333d",
        plan=plan,
        ink="Black",
        profile_id=profile_id,
        extra={
            "data-copy": " | ".join(marker.label for marker, _, _ in placed),
            "data-label-count": str(len(placed)),
            "data-cap-height-mm": format_measurement(cap_mm),
        },
    )
    path_count = 0
    length_mm = 0.0
    for marker, _, strokes in placed:
        path_count += append_vector_strokes(
            group, strokes, {"data-course-marker": marker.kind}
        )
        length_mm += _total_length(strokes)
    _record(
        layer_stats,
        layer_id="course_labels",
        label="Course labels",
        plan=plan,
        ink="Black",
        color="#26333d",
        profile_id=profile_id,
        path_count=path_count,
        length_mm=length_mm,
        group_id="layer-course_labels",
        group_label=group_label,
    )
    return {
        "placed": len(placed),
        "dropped": label_plan.dropped,
        "labels": [marker.label for marker, _, _ in placed],
        "cap_height_mm": round(cap_mm, 4),
    }


def append_race_course(
    root: ET.Element,
    layout: Layout,
    layer_stats: list[dict[str, Any]],
    *,
    course: Any,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
) -> dict[str, Any]:
    """Draw the racing line, and a bar across the water at each end.

    The course is the subject of the plate, so it is drawn last -- over the
    river it follows -- and at the width the plate's ``race_course`` block asks
    for, which is deliberately wider than any single general-colour nib. That
    width is realised the way the road compiler realises a wide road: parallel
    offsets of a real pen, never a fatter line that no pen can draw.
    """

    from .rowing import (
        COURSE_LAYER,
        course_pen_plan,
        end_bar,
        offset_course_strokes,
        project_course,
    )

    if layout.format_id is None:
        raise MapPlotterError("A race course needs a plate format to size its pen.")
    parts = project_course(course, layout)
    if not parts:
        raise MapPlotterError(
            f"Course {course.id!r} does not intersect the map field. Widen the "
            "extent or check the course extent against the requested centre."
        )
    plan = course_pen_plan(
        format_id=layout.format_id,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    nib_mm = plan.pen.mark_width_mm
    minimum_length_mm = 3 * nib_mm
    strokes = offset_course_strokes(
        parts, plan=plan, minimum_length_mm=minimum_length_mm
    )
    bars: list[list[tuple[float, float]]] = []
    for at_start in (True, False):
        bar = end_bar(parts, layout, at_start=at_start)
        if bar is not None and polyline_length(bar) + 1e-9 >= minimum_length_mm:
            bars.append(bar)
    if not strokes:
        raise MapPlotterError(
            f"Course {course.id!r} produced no drawable line above the "
            f"{minimum_length_mm:g} mm physical floor."
        )

    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    colour = plan.pen.preview_color or "#d62828"
    group_label = f"92 — Race course — {plan.pen.label}"
    group = _group(
        root,
        group_id=f"layer-{COURSE_LAYER}",
        label=group_label,
        color=colour,
        plan=plan,
        ink=plan.pen.ink,
        profile_id=profile_id,
        stroke_count=plan.stroke_count,
        offset_pitch_mm=plan.offset_pitch_mm,
        plotted_width_mm=plan.plotted_width_mm,
        extra={
            "data-course-id": str(course.id),
            "data-course-official-distance-m": format_measurement(
                course.official_distance_m
            ),
            "data-course-measured-m": format_measurement(course.measured_length_m),
            "data-course-source": "openstreetmap",
        },
    )
    path_count = append_vector_strokes(group, strokes, {"data-course-part": "line"})
    path_count += append_vector_strokes(group, bars, {"data-course-part": "end-bar"})
    _record(
        layer_stats,
        layer_id=COURSE_LAYER,
        label="Race course",
        plan=plan,
        ink=plan.pen.ink,
        color=colour,
        profile_id=profile_id,
        path_count=path_count,
        length_mm=_total_length(strokes) + _total_length(bars),
        group_id=f"layer-{COURSE_LAYER}",
        group_label=group_label,
        stroke_count=plan.stroke_count,
        offset_pitch_mm=plan.offset_pitch_mm,
        plotted_width_mm=plan.plotted_width_mm,
    )
    return {
        **course.as_dict(),
        "drawn_parts": len(parts),
        "drawn_paths": path_count,
        "pen_id": plan.pen.identity,
        "stroke_count": plan.stroke_count,
        "plotted_width_mm": round(plan.plotted_width_mm, 6),
        "end_bars_drawn": len(bars),
    }


def append_rowing_course_copy(
    root: ET.Element,
    layout: Layout,
    *,
    course: Any,
    layer_stats: list[dict[str, Any]],
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> None:
    """The race plate's header and fact footer, in the memorabilia zones."""

    poster = course.poster
    fields = list(poster["fields"])
    labels = {"person_name": "COURSE"}
    values = {"person_name": str(poster["course_line"])}
    for cell, (label, value) in zip(("degree", "honours", "years"), fields):
        labels[cell] = label
        values[cell] = value
    append_university_memorabilia_copy(
        root,
        layout,
        title=course.title,
        person_name=values.get("person_name"),
        degree=values.get("degree"),
        honours=values.get("honours"),
        years=values.get("years"),
        layer_stats=layer_stats,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
        footer_labels=labels,
        footer_label_text="Course facts",
        subtitle=str(poster.get("subtitle") or ""),
    )


#: The footer cells of the memorabilia composition, left to right, as
#: ``(field id, default label, zone)``.  The race plate reuses the same four
#: cells with its own labels: a fact sheet and a keepsake want the same shape.
MEMORABILIA_FOOTER_CELLS = (
    ("person_name", "NAME", "memorabilia_person_name"),
    ("degree", "DEGREE", "memorabilia_degree"),
    ("honours", "HONOURS", "memorabilia_honours"),
    ("years", "YEARS", "memorabilia_years"),
)

MEMORABILIA_VARIANTS = ("standard", "clean-personalised")


def append_memorabilia_head(
    root: ET.Element,
    layout: Layout,
    *,
    title: str,
    subtitle: str | None,
    layer_stats: list[dict[str, Any]],
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
    zone_names: dict[str, str] | None = None,
    wrap_title: bool = False,
    header_contract: dict[str, Any] | None = None,
) -> None:
    """Serif title, subtitle, coordinates and compass in a named head band.

    Shared by every keepsake composition -- the university plate, the course
    plate, the crew plate and the city-only plate all wear the same head, so a
    customer with two of them on a wall sees one family.
    """

    zone_names = zone_names or {
        "title": "memorabilia_city_title",
        "coordinates": "memorabilia_coordinates",
        "compass": "memorabilia_compass",
    }
    required_zones = set(zone_names.values())
    missing = sorted(required_zones - set(layout.zones))
    if missing:
        raise MapPlotterError(
            "Poster head is missing format-contract zones: "
            + ", ".join(missing)
        )
    display_pen = decoration_pen_plan(
        ink="Black",
        requested_width_mm=0.40,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    text_pen = decoration_pen_plan(
        ink="Black",
        requested_width_mm=0.25,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    type_scale = layout_plate_format(layout)["type_scale_mm"]
    label_cap_mm = float(type_scale["attribution"])

    title_zone = layout.zones[zone_names["title"]]
    title_copy = normalise_text(title).upper()
    preferred_title_height = float(type_scale["title"])
    preferred_title_width = display_text_width_mm(
        title_copy, height_mm=preferred_title_height
    )
    title_height = min(
        preferred_title_height,
        preferred_title_height * title_zone.width_mm / max(preferred_title_width, 1e-9),
    )
    minimum_title_height = 8 * display_pen.pen.mark_width_mm
    if wrap_title:
        minimum_title_height = max(
            minimum_title_height,
            float(layout_plate_format(layout)["rules"]["min_cap_height_mm"]["title"]),
        )

    title_lines: list[tuple[str, list[Stroke]]] = []
    single_line_error: MapPlotterError | None = None
    if title_height + 1e-9 >= minimum_title_height:
        try:
            # Serif overhangs can extend beyond the font's advance width.
            # Fit the actual right ink edge as well as the typographic width.
            probe = display_text(
                title_copy, x_mm=title_zone.x_mm, y_mm=0,
                height_mm=title_height, anchor="start",
            )
            right_extent = max(x for stroke in probe for x, _y in stroke) - title_zone.x_mm
            title_height *= min(
                1.0, (title_zone.width_mm - display_pen.pen.mark_width_mm / 2)
                / max(right_extent, 1e-9),
            )
            if title_height + 1e-9 < minimum_title_height:
                raise MapPlotterError("City title ink cannot fit above its physical cap-height floor.")
            single_strokes = reliable_vector_strokes(
                display_text(
                    title_copy,
                    x_mm=title_zone.x_mm,
                    y_mm=title_zone.y_mm
                    + (title_zone.height_mm - title_height) / 2,
                    height_mm=title_height,
                    anchor="start",
                ),
                nib_mm=display_pen.pen.mark_width_mm,
            )
        except MapPlotterError as exc:
            single_line_error = exc
        else:
            title_lines = [(title_copy, single_strokes)]

    if not title_lines and wrap_title:
        title_rule = layout_plate_format(layout)["rules"]["title_line_layout"]
        if int(title_rule["maximum_lines"]) < 2:
            raise MapPlotterError(
                "The binding title layout does not permit a two-line city title."
            )
        line_gap_mm = float(title_rule["min_path_bounds_gap_mm"])
        horizontal_inset_mm = float(title_rule["horizontal_ink_inset_mm"])
        maximum_line_width_mm = title_zone.width_mm - 2 * horizontal_inset_mm
        words = title_copy.split()
        candidates: list[
            tuple[tuple[float, float], float, list[tuple[str, list[Stroke]]]]
        ] = []
        for split in range(1, len(words)):
            copies = (" ".join(words[:split]), " ".join(words[split:]))
            natural_widths = [
                display_text_width_mm(copy, height_mm=preferred_title_height)
                for copy in copies
            ]
            candidate_height = min(
                preferred_title_height,
                (title_zone.height_mm - line_gap_mm) / 2,
                *(
                    preferred_title_height
                    * maximum_line_width_mm
                    / max(width, 1e-9)
                    for width in natural_widths
                ),
            )
            if candidate_height + 1e-9 < minimum_title_height:
                continue
            block_height = 2 * candidate_height + line_gap_mm
            top = title_zone.y_mm + (title_zone.height_mm - block_height) / 2
            candidate_lines: list[tuple[str, list[Stroke]]] = []
            try:
                for index, copy in enumerate(copies):
                    candidate_lines.append(
                        (
                            copy,
                            reliable_vector_strokes(
                                display_text(
                                    copy,
                                    x_mm=title_zone.x_mm + horizontal_inset_mm,
                                    y_mm=top + index * (candidate_height + line_gap_mm),
                                    height_mm=candidate_height,
                                    anchor="start",
                                ),
                                nib_mm=display_pen.pen.mark_width_mm,
                            ),
                        )
                    )
            except MapPlotterError:
                continue
            imbalance = abs(natural_widths[0] - natural_widths[1])
            candidates.append(
                ((candidate_height, -imbalance), candidate_height, candidate_lines)
            )
        if candidates:
            _score, title_height, title_lines = max(
                candidates, key=lambda candidate: candidate[0]
            )

    if not title_lines:
        if single_line_error is not None and not wrap_title:
            raise single_line_error
        raise MapPlotterError(
            f"Memorabilia title {title!r} cannot fit its left header zone at the "
            f"physical {minimum_title_height:g} mm cap-height floor."
        )

    title_strokes = [
        stroke for _line_copy, line_strokes in title_lines for stroke in line_strokes
    ]
    title_font = display_font_contract()
    title_label = f"96 — Serif city title — {display_pen.pen.label}"
    title_group = _group(
        root,
        group_id="layer-poster_title",
        label=title_label,
        color="#26333d",
        plan=display_pen,
        ink="Black",
        profile_id=profile_id,
        extra={
            "data-copy": title,
            "data-layout-zone": zone_names["title"],
            "data-cap-height-mm": format_measurement(title_height),
            "data-title-lines": str(len(title_lines)),
            "data-title-line-copy-json": json.dumps(
                [line_copy for line_copy, _strokes in title_lines],
                separators=(",", ":"),
            ),
            "data-stroke-font-id": str(title_font["font_id"]),
            "data-stroke-font-sha256": str(title_font["sha256"]),
        },
    )
    if len(title_lines) == 1:
        title_path_count = append_vector_strokes(title_group, title_strokes)
    else:
        title_path_count = 0
        for line_index, (_line_copy, line_strokes) in enumerate(title_lines):
            previous_children = len(title_group)
            append_vector_strokes(
                title_group,
                line_strokes,
                attributes={
                    "data-title-block-id": "city-title",
                    "data-title-line-index": str(line_index),
                    "data-title-line-count": str(len(title_lines)),
                },
            )
            for path in list(title_group)[previous_children:]:
                title_path_count += 1
                path.set("data-physical-stroke", str(title_path_count))
    title_group.set("data-copy-geometry-sha256", stroke_geometry_sha256(title_strokes))
    _record(
        layer_stats,
        layer_id="poster_title",
        label="Serif city title",
        plan=display_pen,
        ink="Black",
        color="#26333d",
        profile_id=profile_id,
        path_count=title_path_count,
        length_mm=_total_length(title_strokes),
        group_id="layer-poster_title",
        group_label=title_label,
    )

    if subtitle and "subtitle" in layout.zones:
        subtitle_zone = layout.zones["subtitle"]
        subtitle_cap = min(
            float(layout_plate_format(layout)["type_scale_mm"]["subtitle"]),
            subtitle_zone.height_mm,
        )
        subtitle_width = text_width_mm(subtitle, cap_height_mm=subtitle_cap)
        if subtitle_width > subtitle_zone.width_mm:
            subtitle_cap = subtitle_cap * subtitle_zone.width_mm / subtitle_width
        if subtitle_cap + 1e-9 >= 8 * text_pen.pen.mark_width_mm:
            subtitle_strokes = reliable_vector_strokes(
                stroke_text(
                    subtitle,
                    x_mm=subtitle_zone.x_mm,
                    y_mm=subtitle_zone.y_mm
                    + (subtitle_zone.height_mm - subtitle_cap) / 2,
                    height_mm=subtitle_cap,
                ),
                nib_mm=text_pen.pen.mark_width_mm,
            )
            subtitle_group_label = f"97 — Course subtitle — {text_pen.pen.label}"
            subtitle_group = _group(
                root,
                group_id="layer-poster_subtitle",
                label=subtitle_group_label,
                color="#58636d",
                plan=text_pen,
                ink="Black",
                profile_id=profile_id,
                extra={
                    "data-copy": subtitle,
                    "data-cap-height-mm": format_measurement(subtitle_cap),
                    "data-copy-geometry-sha256": stroke_geometry_sha256(
                        subtitle_strokes
                    ),
                },
            )
            _record(
                layer_stats,
                layer_id="poster_subtitle",
                label="Course subtitle",
                plan=text_pen,
                ink="Black",
                color="#58636d",
                profile_id=profile_id,
                path_count=append_vector_strokes(subtitle_group, subtitle_strokes),
                length_mm=_total_length(subtitle_strokes),
                group_id="layer-poster_subtitle",
                group_label=subtitle_group_label,
            )

    plate_format = layout_plate_format(layout)
    city_head = zone_names.get("title") == "city_title"
    resolved_header_contract = dict(plate_format.get("city_header", {})) if city_head else {}
    if header_contract is not None:
        resolved_header_contract.update(header_contract)
    if not isinstance(resolved_header_contract, dict):
        raise MapPlotterError("Memorabilia header contract must be an object.")
    coordinate_zone = layout.zones[zone_names["coordinates"]]
    coordinate_height = label_cap_mm
    coordinate_layout = str(
        resolved_header_contract.get("coordinate_layout", "inline")
    )
    coordinate_separator = str(
        resolved_header_contract.get("coordinate_separator", " / ")
    )
    coordinate_tracking_mm = float(
        resolved_header_contract.get("coordinate_tracking_mm", 0.0)
    )
    coordinate_align = str(
        resolved_header_contract.get("coordinate_align", "right")
    )
    coordinate_align_reference = str(
        resolved_header_contract.get("coordinate_align_reference", "zone-edge")
    )
    if coordinate_align not in {"left", "centre", "right"}:
        raise MapPlotterError(
            "Memorabilia coordinate alignment must be left, centre or right."
        )
    if coordinate_align_reference not in {"zone-edge", "title-ink-left"}:
        raise MapPlotterError(
            "Memorabilia coordinate alignment reference must be zone-edge or "
            "title-ink-left."
        )
    coordinate_copy = coordinate_separator.join(coordinate_lines(layout))
    coordinate_copies = (
        coordinate_lines(layout)
        if coordinate_layout == "stacked"
        else (coordinate_copy,)
    )
    coordinate_widths = [
        text_width_mm(
            copy,
            cap_height_mm=coordinate_height,
            tracking_mm=coordinate_tracking_mm,
        )
        for copy in coordinate_copies
    ]
    coordinate_gap_mm = (
        float(resolved_header_contract.get("coordinate_line_gap_mm", 0.0))
        if len(coordinate_copies) > 1
        else 0.0
    )
    coordinate_block_height = (
        len(coordinate_copies) * coordinate_height
        + (len(coordinate_copies) - 1) * coordinate_gap_mm
    )
    if coordinate_block_height > coordinate_zone.height_mm + 1e-9:
        raise MapPlotterError(
            "Map coordinate lines do not fit the binding coordinate zone."
        )
    coordinate_top = coordinate_zone.y_mm + (
        coordinate_zone.height_mm - coordinate_block_height
    ) / 2
    coordinate_x = {
        "left": coordinate_zone.x_mm,
        "centre": coordinate_zone.x_mm + coordinate_zone.width_mm / 2,
        "right": coordinate_zone.x_mm + coordinate_zone.width_mm,
    }[coordinate_align]
    if coordinate_align_reference == "title-ink-left":
        if coordinate_align != "left":
            raise MapPlotterError(
                "Title-ink coordinate alignment requires left-aligned coordinates."
            )
        coordinate_x = min(x for stroke in title_strokes for x, _y in stroke)
    coordinate_available_width = {
        "left": coordinate_zone.x_mm + coordinate_zone.width_mm - coordinate_x,
        "centre": 2
        * min(
            coordinate_x - coordinate_zone.x_mm,
            coordinate_zone.x_mm + coordinate_zone.width_mm - coordinate_x,
        ),
        "right": coordinate_x - coordinate_zone.x_mm,
    }[coordinate_align]
    if max(coordinate_widths) > coordinate_available_width + 1e-9:
        raise MapPlotterError(
            "Map coordinates do not fit the binding coordinate zone at the "
            f"{coordinate_height:g} mm cap height."
        )
    coordinate_anchor = {
        "left": "start",
        "centre": "middle",
        "right": "end",
    }[coordinate_align]
    coordinate_strokes = reliable_vector_strokes(
        [
            stroke
            for index, copy in enumerate(coordinate_copies)
            for stroke in stroke_text(
                copy,
                x_mm=coordinate_x,
                y_mm=coordinate_top
                + index * (coordinate_height + coordinate_gap_mm),
                height_mm=coordinate_height,
                anchor=coordinate_anchor,
                tracking_mm=coordinate_tracking_mm,
            )
        ],
        nib_mm=text_pen.pen.mark_width_mm,
    )
    coordinate_label_text = f"97 — Map coordinates — {text_pen.pen.label}"
    coordinate_group = _group(
        root,
        group_id="layer-poster_coordinates",
        label=coordinate_label_text,
        color="#58636d",
        plan=text_pen,
        ink="Black",
        profile_id=profile_id,
        extra={
            "data-copy": coordinate_copy,
            "data-layout-zone": zone_names["coordinates"],
            "data-cap-height-mm": format_measurement(coordinate_height),
            "data-coordinate-layout": coordinate_layout,
            "data-coordinate-line-copy-json": json.dumps(
                coordinate_copies,
                separators=(",", ":"),
            ),
            "data-coordinate-line-gap-mm": format_measurement(
                coordinate_gap_mm
            ),
            "data-coordinate-tracking-mm": format_measurement(
                coordinate_tracking_mm
            ),
            "data-coordinate-align": coordinate_align,
            "data-coordinate-align-reference": coordinate_align_reference,
        },
    )
    coordinate_path_count = append_vector_strokes(coordinate_group, coordinate_strokes)
    coordinate_group.set(
        "data-copy-geometry-sha256", stroke_geometry_sha256(coordinate_strokes)
    )
    _record(
        layer_stats,
        layer_id="poster_coordinates",
        label="Map coordinates",
        plan=text_pen,
        ink="Black",
        color="#58636d",
        profile_id=profile_id,
        path_count=coordinate_path_count,
        length_mm=_total_length(coordinate_strokes),
        group_id="layer-poster_coordinates",
        group_label=coordinate_label_text,
    )

    compass_zone = layout.zones[zone_names["compass"]]
    # Draw at the contract's preferred size inside the full header column.
    # Position the finished mark from actual lettering bounds below, so short
    # names and wrapped names both balance against the same north mark.
    compass_column = compass_zone
    compass_zone = replace(
        compass_zone,
        height_mm=float(resolved_header_contract.get(
            "compass_draw_height_mm", compass_zone.height_mm
        )),
    )
    compass_pen = display_pen
    if resolved_header_contract:
        compass_pen = decoration_pen_plan(
            ink="Black",
            requested_width_mm=float(
                resolved_header_contract.get(
                    "compass_nib_mm", display_pen.pen.mark_width_mm
                )
            ),
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
    compass_cap_mm = min(
        float(type_scale["subtitle"]) * 1.4, compass_zone.height_mm / 3
    )
    compass_x = compass_zone.x_mm + compass_zone.width_mm / 2
    compass_top = compass_zone.y_mm + compass_cap_mm + 0.8
    compass_bottom = compass_zone.y_mm + compass_zone.height_mm - 0.5
    compass_middle = (compass_top + compass_bottom) / 2
    compass_style = str(
        resolved_header_contract.get("compass_style", "diamond-cardinal")
    )
    compass_geometry = resolved_header_contract.get("compass_geometry", {})
    if not isinstance(compass_geometry, dict):
        raise MapPlotterError("City compass geometry must be an object.")
    drawable_height = compass_bottom - compass_top
    compass_components: dict[str, list[Stroke]]
    if compass_style == "diamond-cardinal":
        compass_half = min(
            compass_zone.width_mm * 0.22,
            max(0.0, drawable_height) * 0.42,
        )
        compass_components = {
            "cardinal-diamond": [
                [
                    (compass_x, compass_top),
                    (compass_x + compass_half, compass_middle),
                    (compass_x, compass_bottom),
                    (compass_x - compass_half, compass_middle),
                    (compass_x, compass_top),
                ],
                [(compass_x, compass_top), (compass_x, compass_bottom)],
                [
                    (compass_x - compass_half, compass_middle),
                    (compass_x + compass_half, compass_middle),
                ],
            ]
        }
    elif compass_style == "north-arrow-east-west-axis":
        geometry_keys = {
            "arrow_head_half_width_zone_fraction",
            "arrow_head_height_drawable_fraction",
            "east_west_half_width_zone_fraction",
            "east_west_crossing_drawable_fraction",
        }
        if set(compass_geometry) != geometry_keys:
            raise MapPlotterError(
                "North-arrow city compass geometry does not match the binding "
                "four-value contract."
            )
        values = {key: float(compass_geometry[key]) for key in geometry_keys}
        if not all(0.0 < value < 1.0 for value in values.values()):
            raise MapPlotterError(
                "North-arrow city compass fractions must lie strictly between 0 and 1."
            )
        if (
            values["arrow_head_half_width_zone_fraction"] >= 0.5
            or values["east_west_half_width_zone_fraction"] >= 0.5
            or values["arrow_head_height_drawable_fraction"]
            >= values["east_west_crossing_drawable_fraction"]
        ):
            raise MapPlotterError(
                "North-arrow city compass geometry would leave its zone or put "
                "the east-west axis inside the arrowhead."
            )
        arrow_half_width = (
            compass_zone.width_mm
            * values["arrow_head_half_width_zone_fraction"]
        )
        arrow_head_bottom = (
            compass_top
            + drawable_height
            * values["arrow_head_height_drawable_fraction"]
        )
        east_west_half_width = (
            compass_zone.width_mm
            * values["east_west_half_width_zone_fraction"]
        )
        east_west_y = (
            compass_top
            + drawable_height
            * values["east_west_crossing_drawable_fraction"]
        )
        compass_components = {
            "north-arrow-shaft": [
                [
                    (compass_x, compass_bottom),
                    (compass_x, compass_top),
                ]
            ],
            "north-arrow-head": [
                [
                    (compass_x - arrow_half_width, arrow_head_bottom),
                    (compass_x, compass_top),
                    (compass_x + arrow_half_width, arrow_head_bottom),
                ]
            ],
            "east-west-axis": [
                [
                    (compass_x - east_west_half_width, east_west_y),
                    (compass_x + east_west_half_width, east_west_y),
                ]
            ],
        }
    else:
        raise MapPlotterError(f"Unsupported city compass style: {compass_style!r}.")

    north_strokes = reliable_vector_strokes(
        stroke_text(
            "N",
            x_mm=compass_x,
            y_mm=compass_zone.y_mm,
            height_mm=compass_cap_mm,
            anchor="middle",
        ),
        nib_mm=compass_pen.pen.mark_width_mm,
    )
    reliable_components = {
        component: reliable_vector_strokes(
            strokes,
            nib_mm=compass_pen.pen.mark_width_mm,
        )
        for component, strokes in compass_components.items()
    }
    compass_align_reference = resolved_header_contract.get("compass_align_reference", "zone")
    if compass_align_reference == "header-ink-centre":
        header_top = min(y for stroke in title_strokes for _x, y in stroke) - display_pen.pen.mark_width_mm / 2
        header_bottom = max(y for stroke in coordinate_strokes for _x, y in stroke) + text_pen.pen.mark_width_mm / 2
        all_compass = [*north_strokes, *(s for strokes in reliable_components.values() for s in strokes)]
        top = min(y for stroke in all_compass for _x, y in stroke)
        bottom = max(y for stroke in all_compass for _x, y in stroke)
        shift_y = (header_top + header_bottom - top - bottom) / 2
        half_nib = compass_pen.pen.mark_width_mm / 2
        if (top + shift_y - half_nib < compass_column.y_mm - 1e-9
                or bottom + shift_y + half_nib > compass_column.y_mm + compass_column.height_mm + 1e-9):
            raise MapPlotterError("Centred city compass does not fit its header column.")
        north_strokes = [[(x, y + shift_y) for x, y in stroke] for stroke in north_strokes]
        reliable_components = {
            name: [[(x, y + shift_y) for x, y in stroke] for stroke in strokes]
            for name, strokes in reliable_components.items()
        }
    compass_strokes = [
        *north_strokes,
        *(
            stroke
            for component_strokes in reliable_components.values()
            for stroke in component_strokes
        ),
    ]
    compass_label = f"93 — Header compass — {compass_pen.pen.label}"
    compass_group = _group(
        root,
        group_id="layer-poster_compass",
        label=compass_label,
        color="#26333d",
        plan=compass_pen,
        ink="Black",
        profile_id=profile_id,
        extra={
            "data-copy": "N",
            "data-layout-zone": zone_names["compass"],
            "data-compass-style": compass_style,
            "data-compass-align-reference": str(compass_align_reference),
            "data-cardinal-axes": "north,east-west",
            "data-compass-geometry-json": json.dumps(
                compass_geometry,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    )
    compass_path_count = append_vector_strokes(
        compass_group,
        north_strokes,
        {"data-compass-component": "north-label"},
    )
    for component, strokes in reliable_components.items():
        compass_path_count += append_vector_strokes(
            compass_group,
            strokes,
            {"data-compass-component": component},
        )
    _record(
        layer_stats,
        layer_id="poster_compass",
        label="Header compass",
        plan=compass_pen,
        ink="Black",
        color="#26333d",
        profile_id=profile_id,
        path_count=compass_path_count,
        length_mm=_total_length(compass_strokes),
        group_id="layer-poster_compass",
        group_label=compass_label,
    )


def append_city_map_copy(
    root: ET.Element,
    layout: Layout,
    *,
    title: str,
    layer_stats: list[dict[str, Any]],
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> None:
    """Draw the city-only head without any university or personal footer."""

    append_memorabilia_head(
        root,
        layout,
        title=title,
        subtitle=None,
        layer_stats=layer_stats,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
        zone_names={
            "title": "city_title",
            "coordinates": "city_coordinates",
            "compass": "city_compass",
        },
        wrap_title=True,
    )


def append_university_memorabilia_copy(
    root: ET.Element,
    layout: Layout,
    *,
    title: str,
    person_name: str | None,
    degree: str | None,
    honours: str | None,
    years: str | None,
    layer_stats: list[dict[str, Any]],
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
    footer_labels: dict[str, str] | None = None,
    footer_label_text: str = "Personalisation fields",
    subtitle: str | None = None,
    memorabilia_variant: str = "standard",
) -> None:
    """Draw the keepsake head and its four-cell footer.

    ``footer_labels`` relabels the cells without moving them, which is how the
    rowing course plate prints COURSE / DISTANCE / FIRST ROWED / BOATS in the
    same zones the university plate leaves blank for a pen.
    """

    if memorabilia_variant not in MEMORABILIA_VARIANTS:
        raise MapPlotterError(
            "Memorabilia variant must be one of: "
            + ", ".join(MEMORABILIA_VARIANTS)
            + "."
        )
    variant_contract: dict[str, Any] | None = None
    if memorabilia_variant == "clean-personalised":
        raw_variants = layout_plate_format(layout).get("memorabilia_variants")
        if not isinstance(raw_variants, dict):
            raise MapPlotterError(
                "The binding plate has no personalised memorabilia variants."
            )
        raw_variant = raw_variants.get(memorabilia_variant)
        if not isinstance(raw_variant, dict):
            raise MapPlotterError(
                f"The binding plate has no {memorabilia_variant!r} variant."
            )
        variant_contract = raw_variant
        raw_header = variant_contract.get("header")
        if not isinstance(raw_header, dict):
            raise MapPlotterError(
                "Clean personalised memorabilia has no header contract."
            )
        append_memorabilia_head(
            root,
            layout,
            title=title,
            subtitle=subtitle,
            layer_stats=layer_stats,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
            zone_names={
                "title": str(raw_header["title_zone"]),
                "coordinates": str(raw_header["coordinates_zone"]),
                "compass": str(raw_header["compass_zone"]),
            },
            wrap_title=bool(raw_header.get("title_wrap", False)),
            header_contract=raw_header,
        )
    elif footer_labels is None and not subtitle:
        append_city_map_copy(
            root, layout, title=title, layer_stats=layer_stats,
            pen_inventory=pen_inventory, allowed_nibs_mm=allowed_nibs_mm,
        )
    else:
        append_memorabilia_head(
            root,
            layout,
            title=title,
            subtitle=subtitle,
            layer_stats=layer_stats,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
    text_pen = decoration_pen_plan(
        ink="Black",
        requested_width_mm=0.25,
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    profile_id = pen_inventory.id if pen_inventory is not None else "style"
    type_scale = layout_plate_format(layout)["type_scale_mm"]
    label_cap_mm = float(type_scale["attribution"])
    value_cap_mm = float(type_scale["detail"])

    personalisation = {
        "person_name": person_name or "",
        "degree": degree or "",
        "honours": honours or "",
        "years": years or "",
    }
    if memorabilia_variant == "clean-personalised":
        assert variant_contract is not None
        raw_footer = variant_contract.get("footer")
        if not isinstance(raw_footer, dict):
            raise MapPlotterError(
                "Clean personalised memorabilia has no footer contract."
            )
        if raw_footer.get("honours_policy") == "must-be-empty" and honours:
            raise MapPlotterError(
                "The clean personalised memorabilia footer has no honours cell; "
                "omit --honours or use the standard variant."
            )
        if raw_footer.get("show_labels") is not False:
            raise MapPlotterError(
                "Clean personalised memorabilia must suppress footer labels."
            )
        if raw_footer.get("show_write_lines") is not False:
            raise MapPlotterError(
                "Clean personalised memorabilia must suppress writing rules."
            )
        visible_fields = raw_footer.get("visible_fields")
        field_zone_spans = raw_footer.get("field_zone_spans")
        field_align = raw_footer.get("field_align")
        if (
            not isinstance(visible_fields, list)
            or not isinstance(field_zone_spans, dict)
            or not isinstance(field_align, dict)
        ):
            raise MapPlotterError(
                "Clean personalised memorabilia footer fields are invalid."
            )
        expected_fields = ["person_name", "degree", "years"]
        if visible_fields != expected_fields:
            raise MapPlotterError(
                "Clean personalised memorabilia must draw name, degree and years."
            )
        value_cap_role = str(raw_footer.get("value_cap_role", "detail"))
        if value_cap_role not in type_scale:
            raise MapPlotterError(
                "Clean personalised memorabilia names an unknown type role."
            )
        value_font_role = str(raw_footer.get("value_font_role", ""))
        if value_font_role != "display-serif":
            raise MapPlotterError(
                "Clean personalised memorabilia must use the display-serif footer."
            )
        value_pen = decoration_pen_plan(
            ink="Black",
            requested_width_mm=float(raw_footer.get("value_nib_mm", 0.0)),
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        base_value_cap_mm = float(type_scale[value_cap_role])
        name_cap_multiplier = float(raw_footer.get("name_cap_multiplier", 1.0))
        secondary_cap_multiplier = float(
            raw_footer.get("secondary_cap_multiplier", 1.0)
        )
        if name_cap_multiplier <= 0 or secondary_cap_multiplier <= 0:
            raise MapPlotterError(
                "Clean personalised footer cap multipliers must be positive."
            )
        inset_mm = float(raw_footer.get("value_inset_mm", 0.0))
        horizontal_bounds_zone_name = str(
            raw_footer.get("horizontal_bounds_zone", "map_field")
        )
        if horizontal_bounds_zone_name not in layout.zones:
            raise MapPlotterError(
                "Clean personalised footer horizontal bounds zone is missing."
            )
        horizontal_bounds_zone = layout.zones[horizontal_bounds_zone_name]
        clean_font = display_font_contract()
        clean_label = f"98 — Personalised details — {value_pen.pen.label}"
        clean_group = _group(
            root,
            group_id="layer-poster_personalisation",
            label=clean_label,
            color="#58636d",
            plan=value_pen,
            ink="Black",
            profile_id=profile_id,
            extra={
                "data-fields-json": json.dumps(
                    personalisation, sort_keys=True, separators=(",", ":")
                ),
                "data-memorabilia-variant": memorabilia_variant,
                "data-labels-visible": "false",
                "data-write-lines-visible": "false",
                "data-value-font-role": value_font_role,
                "data-stroke-font-id": str(clean_font["font_id"]),
                "data-stroke-font-sha256": str(clean_font["sha256"]),
                "data-horizontal-bounds-zone": horizontal_bounds_zone_name,
            },
        )
        clean_path_count = 0
        clean_length_mm = 0.0
        clean_copy_strokes: list[Stroke] = []
        for field_id in visible_fields:
            value = personalisation[field_id]
            if not value:
                continue
            raw_zone_span = field_zone_spans.get(field_id)
            if not isinstance(raw_zone_span, list) or not raw_zone_span:
                raise MapPlotterError(
                    f"Clean personalised footer field {field_id!r} has no zone span."
                )
            zone_names = [str(zone_name) for zone_name in raw_zone_span]
            missing_zones = [
                zone_name for zone_name in zone_names if zone_name not in layout.zones
            ]
            if missing_zones:
                raise MapPlotterError(
                    "Clean personalised footer zones are missing: "
                    + ", ".join(missing_zones)
                    + "."
                )
            zones = [layout.zones[zone_name] for zone_name in zone_names]
            zone_x = min(zone.x_mm for zone in zones)
            zone_y = min(zone.y_mm for zone in zones)
            zone_right = max(zone.x_mm + zone.width_mm for zone in zones)
            zone_bottom = max(zone.y_mm + zone.height_mm for zone in zones)
            zone_width = zone_right - zone_x
            zone_height = zone_bottom - zone_y
            align = str(field_align[field_id])
            if align not in {"left", "centre", "right"}:
                raise MapPlotterError(
                    f"Clean personalised footer alignment {align!r} is invalid."
                )
            requested_cap_mm = base_value_cap_mm * (
                name_cap_multiplier
                if field_id == "person_name"
                else secondary_cap_multiplier
            )
            maximum_width = zone_width - 2 * inset_mm
            natural_width = display_text_width_mm(
                value, height_mm=requested_cap_mm
            )
            value_height = min(
                requested_cap_mm,
                requested_cap_mm * maximum_width / max(natural_width, 1e-9),
            )
            floor_mm = 8 * value_pen.pen.mark_width_mm
            if value_height + 1e-9 < floor_mm:
                raise MapPlotterError(
                    f"Footer value {value!r} cannot fit field {field_id!r} at "
                    f"the physical {floor_mm:g} mm cap-height floor. Shorten it."
                )
            x_mm = {
                "left": zone_x + inset_mm,
                "centre": zone_x + zone_width / 2,
                "right": zone_right - inset_mm,
            }[align]
            anchor = {"left": "start", "centre": "middle", "right": "end"}[
                align
            ]
            vertical = str(raw_footer.get("value_vertical", "middle"))
            if vertical != "middle":
                raise MapPlotterError(
                    "Clean personalised footer values must use middle placement."
                )
            y_mm = zone_y + (zone_height - value_height) / 2
            value_strokes = reliable_vector_strokes(
                display_text(
                    value,
                    x_mm=x_mm,
                    y_mm=y_mm,
                    height_mm=value_height,
                    anchor=anchor,
                ),
                nib_mm=value_pen.pen.mark_width_mm,
            )
            ink_xs = [x for stroke in value_strokes for x, _y in stroke]
            half_nib_mm = value_pen.pen.mark_width_mm / 2
            if (
                min(ink_xs) - half_nib_mm
                < horizontal_bounds_zone.x_mm - 1e-9
                or max(ink_xs) + half_nib_mm
                > horizontal_bounds_zone.x_mm
                + horizontal_bounds_zone.width_mm
                + 1e-9
            ):
                raise MapPlotterError(
                    f"Footer value {value!r} crosses the horizontal bounds of "
                    f"{horizontal_bounds_zone_name!r}."
                )
            clean_path_count += append_vector_strokes(
                clean_group,
                value_strokes,
                {
                    "data-personalisation-field": field_id,
                    "data-field-part": "value",
                    "data-field-align": align,
                    "data-layout-zone-span-json": json.dumps(
                        zone_names, separators=(",", ":")
                    ),
                    "data-cap-height-mm": format_measurement(value_height),
                },
            )
            clean_copy_strokes.extend(value_strokes)
            clean_length_mm += _total_length(value_strokes)
        clean_group.set(
            "data-copy-geometry-sha256",
            stroke_geometry_sha256(clean_copy_strokes),
        )
        _record(
            layer_stats,
            layer_id="poster_personalisation",
            label="Personalised details",
            plan=value_pen,
            ink="Black",
            color="#58636d",
            profile_id=profile_id,
            path_count=clean_path_count,
            length_mm=clean_length_mm,
            group_id="layer-poster_personalisation",
            group_label=clean_label,
        )
        return

    labels = footer_labels or {}
    fields = tuple(
        (field_id, labels.get(field_id, default_label), zone_id)
        for field_id, default_label, zone_id in MEMORABILIA_FOOTER_CELLS
    )
    personal_label = f"98 — {footer_label_text} — {text_pen.pen.label}"
    personal_group = _group(
        root,
        group_id="layer-poster_personalisation",
        label=personal_label,
        color="#58636d",
        plan=text_pen,
        ink="Black",
        profile_id=profile_id,
        extra={
            "data-fields-json": json.dumps(
                personalisation, sort_keys=True, separators=(",", ":")
            )
        },
    )
    personal_path_count = 0
    personal_length_mm = 0.0
    copy_strokes: list[Stroke] = []
    for field_id, label, zone_id in fields:
        zone = layout.zones[zone_id]
        label_strokes = reliable_vector_strokes(
            stroke_text(
                label,
                x_mm=zone.x_mm + 0.5,
                y_mm=zone.y_mm + 0.5,
                height_mm=label_cap_mm,
            ),
            nib_mm=text_pen.pen.mark_width_mm,
        )
        personal_path_count += append_vector_strokes(
            personal_group,
            label_strokes,
            {"data-personalisation-field": field_id, "data-field-part": "label"},
        )
        copy_strokes.extend(label_strokes)
        personal_length_mm += _total_length(label_strokes)
        rule_y = zone.y_mm + zone.height_mm - 1.0
        rule = [(zone.x_mm + 0.5, rule_y), (zone.x_mm + zone.width_mm - 0.5, rule_y)]
        ET.SubElement(
            personal_group,
            svg_tag("path"),
            {
                "d": path_data(rule),
                "data-personalisation-field": field_id,
                "data-field-part": "write-line",
            },
        )
        personal_path_count += 1
        personal_length_mm += polyline_length(rule)
        value = personalisation[field_id]
        if not value:
            continue
        maximum_width = zone.width_mm - 2.0
        natural_width = text_width_mm(value, cap_height_mm=value_cap_mm)
        value_height = min(
            value_cap_mm, value_cap_mm * maximum_width / max(natural_width, 1e-9)
        )
        floor_mm = 8 * text_pen.pen.mark_width_mm
        if value_height + 1e-9 < floor_mm:
            raise MapPlotterError(
                f"Footer value {value!r} cannot fit field {field_id!r} at the "
                f"physical {floor_mm:g} mm cap-height floor. Shorten it."
            )
        value_strokes = reliable_vector_strokes(
            stroke_text(
                value,
                x_mm=zone.x_mm + 0.5,
                y_mm=rule_y - value_height - 1.0,
                height_mm=value_height,
            ),
            nib_mm=text_pen.pen.mark_width_mm,
        )
        personal_path_count += append_vector_strokes(
            personal_group,
            value_strokes,
            {"data-personalisation-field": field_id, "data-field-part": "value"},
        )
        copy_strokes.extend(value_strokes)
        personal_length_mm += _total_length(value_strokes)
    personal_group.set(
        "data-copy-geometry-sha256", stroke_geometry_sha256(copy_strokes)
    )
    _record(
        layer_stats,
        layer_id="poster_personalisation",
        label=footer_label_text,
        plan=text_pen,
        ink="Black",
        color="#58636d",
        profile_id=profile_id,
        path_count=personal_path_count,
        length_mm=personal_length_mm,
        group_id="layer-poster_personalisation",
        group_label=personal_label,
    )
