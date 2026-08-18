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
from .geometry import A5_POSTER_PRESETS, Layout, Rect, load_plate_format, polyline_length
from .models import MapPlotterError
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
    wrapped = _two_line_split(
        text, maximum_width_mm, cap_height_mm=minimum_height_mm
    )
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
        if _fit_cap(lines[0], maximum_width_mm=maximum_width, cap_height_mm=cap) + 1e-9 < minimum_cap:
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


def weighted(strokes: list[Stroke], *, placement_or_count: int, nib_mm: float) -> list[Stroke]:
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
        block = _attribution_block(layout, placement)
        return (
            reliable_vector_strokes(
                weighted(block, placement_or_count=weight, nib_mm=nib_mm),
                nib_mm=nib_mm,
            ),
            1,
        )

    line_step = _detail_line_step(layout, placement) if role == "detail" else None
    block = set_text_block(
        source_copy, layout=layout, placement=placement, line_step_mm=line_step
    )
    return (
        reliable_vector_strokes(
            weighted(block.strokes, placement_or_count=weight, nib_mm=nib_mm),
            nib_mm=nib_mm,
        ),
        block.line_count,
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
            north_paths = [
                path for path in paths if path.get("data-north") is not None
            ]
            scale_paths = [path for path in paths if path.get("data-north") is None]
            if north_paths:
                checks.append(("map_field", layout.zones["map_field"], north_paths))
            if scale_paths:
                scale_zone = legend_zone(layout, placement)
                scale_zone_name = (
                    "furniture" if zone_name == "map_field" else zone_name
                )
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


def coordinate_label(layout: Layout) -> str:
    latitude, longitude = layout.bbox.center
    return (
        f"{abs(latitude):.4f} {'N' if latitude >= 0 else 'S'} / "
        f"{abs(longitude):.4f} {'E' if longitude >= 0 else 'W'}"
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
    usable = (
        usable_zone(zone, placement) if placement is not None else zone
    )
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
    return (
        f"{distance_m / 1_000:g} KM" if distance_m >= 1_000 else f"{distance_m:g} M"
    )


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
    plate = (
        layout_plate_format(layout) if layout.format_id is not None else None
    )
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
        layout.page.height_mm
        - layout.margin_mm
        - (layout.footer_mm + block_height) / 2
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
    layer_stats: list[dict[str, Any]],
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
            "poster_border", "hairline" if policy.border_style == "hairline" else "heavy"
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

    if poster_layout == "university-memorabilia":
        append_university_memorabilia_copy(
            root,
            layout,
            title=title,
            person_name=person_name,
            degree=degree,
            honours=honours,
            years=years,
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
    pitch_mm, plotted_width_mm = type_weight_plan(
        stroke_count=weight, nib_mm=nib_mm
    )
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
) -> None:
    """Draw the asymmetric university keepsake header and writable footer."""

    required_zones = {
        "memorabilia_city_title",
        "memorabilia_coordinates",
        "memorabilia_compass",
        "memorabilia_person_name",
        "memorabilia_degree",
        "memorabilia_honours",
        "memorabilia_years",
    }
    missing = sorted(required_zones - set(layout.zones))
    if missing:
        raise MapPlotterError(
            "University memorabilia layout is missing format-contract zones: "
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

    title_zone = layout.zones["memorabilia_city_title"]
    title_copy = normalise_text(title).upper()
    preferred_title_height = 7.0
    preferred_title_width = display_text_width_mm(
        title_copy, height_mm=preferred_title_height
    )
    title_height = min(
        preferred_title_height,
        preferred_title_height * title_zone.width_mm / max(preferred_title_width, 1e-9),
    )
    minimum_title_height = 8 * display_pen.pen.mark_width_mm
    if title_height + 1e-9 < minimum_title_height:
        raise MapPlotterError(
            f"Memorabilia title {title!r} cannot fit its left header zone at the "
            f"physical {minimum_title_height:g} mm cap-height floor."
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
            "data-cap-height-mm": format_measurement(title_height),
            "data-stroke-font-id": str(title_font["font_id"]),
            "data-stroke-font-sha256": str(title_font["sha256"]),
        },
    )
    title_path_count = append_vector_strokes(title_group, title_strokes)
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

    coordinate_zone = layout.zones["memorabilia_coordinates"]
    coordinate_copy = coordinate_label(layout)
    coordinate_height = 2.0
    coordinate_width = text_width_mm(coordinate_copy, cap_height_mm=coordinate_height)
    if coordinate_width > coordinate_zone.width_mm + 1e-9:
        raise MapPlotterError(
            "Map coordinates do not fit the memorabilia coordinate zone at the "
            "binding 2 mm cap height."
        )
    coordinate_strokes = reliable_vector_strokes(
        stroke_text(
            coordinate_copy,
            x_mm=coordinate_zone.x_mm + coordinate_zone.width_mm,
            y_mm=coordinate_zone.y_mm + 0.5,
            height_mm=coordinate_height,
            anchor="end",
        ),
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
            "data-cap-height-mm": format_measurement(coordinate_height),
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

    compass_zone = layout.zones["memorabilia_compass"]
    compass_x = compass_zone.x_mm + compass_zone.width_mm / 2
    compass_top = compass_zone.y_mm + 4.0
    compass_bottom = compass_zone.y_mm + compass_zone.height_mm - 0.5
    compass_middle = (compass_top + compass_bottom) / 2
    compass_paths = [
        [
            (compass_x, compass_top),
            (compass_x + 2.2, compass_middle),
            (compass_x, compass_bottom),
            (compass_x - 2.2, compass_middle),
            (compass_x, compass_top),
        ],
        [(compass_x, compass_top), (compass_x, compass_bottom)],
        [(compass_x - 2.2, compass_middle), (compass_x + 2.2, compass_middle)],
    ]
    north_strokes = stroke_text(
        "N",
        x_mm=compass_x,
        y_mm=compass_zone.y_mm,
        height_mm=3.2,
        anchor="middle",
    )
    compass_strokes = reliable_vector_strokes(
        [*north_strokes, *compass_paths],
        nib_mm=display_pen.pen.mark_width_mm,
    )
    compass_label = f"93 — Header compass — {display_pen.pen.label}"
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
    compass_path_count = append_vector_strokes(compass_group, compass_strokes)
    _record(
        layer_stats,
        layer_id="poster_compass",
        label="Header compass",
        plan=display_pen,
        ink="Black",
        color="#26333d",
        profile_id=profile_id,
        path_count=compass_path_count,
        length_mm=_total_length(compass_strokes),
        group_id="layer-poster_compass",
        group_label=compass_label,
    )

    personalisation = {
        "person_name": person_name or "",
        "degree": degree or "",
        "honours": honours or "",
        "years": years or "",
    }
    fields = (
        ("person_name", "NAME", "memorabilia_person_name"),
        ("degree", "DEGREE", "memorabilia_degree"),
        ("honours", "HONOURS", "memorabilia_honours"),
        ("years", "YEARS", "memorabilia_years"),
    )
    personal_label = f"98 — Personalisation fields — {text_pen.pen.label}"
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
                height_mm=2.0,
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
        natural_width = text_width_mm(value, cap_height_mm=2.35)
        value_height = min(2.35, 2.35 * maximum_width / max(natural_width, 1e-9))
        if value_height + 1e-9 < 2.0:
            raise MapPlotterError(
                f"Personalisation value {value!r} cannot fit field {field_id!r} "
                "at the physical 2 mm cap-height floor."
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
        label="Personalisation fields",
        plan=text_pen,
        ink="Black",
        color="#58636d",
        profile_id=profile_id,
        path_count=personal_path_count,
        length_mm=personal_length_mm,
        group_id="layer-poster_personalisation",
        group_label=personal_label,
    )
