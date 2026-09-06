#!/usr/bin/env python3
"""Generate the canonical plate-format specification (format-v1.json).

Every number in the shipped specification is DERIVED here from a small set of
rules, not hand-typed per sheet. That is the whole point: A5, A4 and A3 stay
visually identical in proportion, and a reviewer can check the rule instead of
checking 6 x 12 magic numbers.

Run:  python3 tools/build_format_spec.py
Out:  docs/format/format-v1.json
      src/city_map_plotter/data/format-v1.json
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = 1

ROOT = Path(__file__).resolve().parent.parent
AUTHORITATIVE_SPEC_PATH = ROOT / "docs" / "format" / "format-v1.json"
PACKAGED_SPEC_PATH = ROOT / "src" / "city_map_plotter" / "data" / "format-v1.json"
SPEC_OUTPUT_PATHS = (AUTHORITATIVE_SPEC_PATH, PACKAGED_SPEC_PATH)

# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
# Sheet sizes, portrait (short, long).
SHEETS = {"A5": (148.0, 210.0), "A4": (210.0, 297.0), "A3": (297.0, 420.0)}

# Plotter-safe border. Machine clamps do not scale with paper, so this is a
# proportional rule with a hard floor and ceiling rather than a pure ratio.
SAFE_RATIO, SAFE_MIN, SAFE_MAX = 0.0405, 6.0, 12.0

# Title cap height as a fraction of the SHORT edge. Chosen so A5 reproduces the
# 7.0 mm title the existing hand-tuned poster already uses; A4/A3 then follow
# the A-series 1:sqrt(2) progression automatically.
TITLE_RATIO = 0.0473

# Typographic ladder, expressed as multiples of the title cap height.
# The A5 column of this ladder reproduces the existing poster almost exactly
# (subtitle 2.15, detail 2.35) which is why these ratios were adopted rather
# than invented.
TYPE_ROLES = {
    "title": 1.000,
    "detail": 0.336,
    # A4 uses the real 0.40 mm general-colour pen for small display text.  The
    # 0.323 ratio lands just above the binding 8 x nib cap-height floor.
    "subtitle": 0.323,
    "legend": 0.323,
    # The smallest physical pen in the studio inventory is 0.25 mm, so A5
    # attribution must be at least 2.0 mm rather than the legacy 1.6 mm.
    "attribution": 0.286,
}

# Legibility floor: a stroke-font cap height below 8x the nib that draws it
# closes up and reads as a blot. Enforced by the validator.
MIN_CAP_HEIGHT_NIB_MULTIPLE = 8.0

# Distinct title lines need white paper between their *ink envelopes*, not
# merely between the cap-height boxes used by the stroke-font compositor. One
# full nib of white paper remains visibly open after ordinary pen spread. The
# resolved per-format rule below adds one more full nib for the two half-width
# ink envelopes. Title-zone height is raised where needed so this is a physical
# contract rather than an optimistic centreline-only fit.
TITLE_LINE_INK_CLEARANCE_NIB_MULTIPLE = 1.0

# Sub-nib geometry gate: nothing shorter than this survives to the pen.
MIN_STROKE_NIB_MULTIPLE = 3.0

# The actual studio inventory is the physical contract.  Every listed width is
# available in at least one ink: 0.25/0.40 in the general colour set,
# 0.30/0.40/0.50 plus 0.70/1.00 in white, 0.60/1.00 in black, and 1.00 in
# gold/silver.  Ink-specific
# availability is enforced by the renderer's pen inventory; this sheet ladder
# states which physical nibs a generated plate may request.
NIB_LADDER = {
    # Width alone is not permission to use any colour: the renderer's named
    # inventory additionally enforces that 0.30/0.50/0.70 are white-only,
    # 0.60 is black-only, and 1.00 is limited to black/white/gold/silver.
    "A5": [0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 1.00],
    "A4": [0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 1.00],
    "A3": [0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 1.00],
}

# Page-specific roles choose only pens that exist in the inventory and keep
# display type above the 8 x nib legibility floor. Duplicate role widths are
# deliberate where one physical pen best serves two visual roles.
# Type and furniture nibs. These DO scale with the sheet: cap heights grow with
# the paper, and the 8 x nib legibility floor would leave large lettering
# spidery if the nib stayed put.
NIB_ROLES = {
    "A5": {"hairline": 0.25, "text": 0.25, "primary": 0.40, "heavy": 0.60},
    "A4": {"hairline": 0.25, "text": 0.40, "primary": 0.60, "heavy": 1.00},
    "A3": {"hairline": 0.40, "text": 0.40, "primary": 0.60, "heavy": 1.00},
}

# Map LINEWORK nibs. These deliberately do NOT scale the same way, and the
# reason is measured rather than aesthetic. On the York all-roads plate, 89% of
# all ink length sits in the two finest roles (hairline 55%, text 34%); primary
# and heavy together are 11%. Scaling the fine end with the sheet therefore
# cancels almost the entire area gain of moving to bigger paper -- A3 came out
# at 33.4% ink coverage, no better than A5. Holding the fine end at the
# narrowest real nib and scaling only the hierarchy-carrying roles takes the
# same plate to 27.2%, which is plottable.
MAP_LINEWORK_NIBS = {
    "A5": {"hairline": 0.25, "text": 0.25, "primary": 0.40, "heavy": 0.60},
    "A4": {"hairline": 0.25, "text": 0.25, "primary": 0.60, "heavy": 1.00},
    "A3": {"hairline": 0.25, "text": 0.40, "primary": 0.60, "heavy": 1.00},
}

# Ink coverage = sum(stroke_length * nib) / map field area. Above roughly a
# third of the field the gaps between adjacent lines fall below the nib and the
# map reads as grey wash instead of a hierarchy. 0.28 leaves headroom for ink
# spread on absorbent stock.
MAX_INK_COVERAGE = 0.28

# Landmark buildings are a capped, named-object feature rather than a bulk
# layer, so they get their own per-sheet policy instead of inheriting the road
# hierarchy's. Two independent levers:
#
#   nib_role      -- weight. Named against map_linework_nib_mm, so the fine end
#                    stays held where that table holds it. A5 keeps buildings on
#                    the narrowest pen; the bigger sheets can afford the next
#                    step up where that table offers one.
#   the rest      -- inclusion. A5's field is half A4's, so the same object
#                    count and outline budget that reads as a legible set of
#                    landmarks on A4 silts up the smaller sheet. The budget
#                    fraction is already a share of field area; these multiply
#                    that so the larger sheet gets proportionally MORE, not just
#                    absolutely more.
#
# `minimum_area_scale` multiplies the per-role footprint thresholds: above 1 a
# landmark must be relatively larger to earn its place.
LANDMARK_BUILDINGS = {
    "A5": {
        "nib_role": "hairline",
        "ink_budget_field_fraction": 0.004,
        "max_objects": 12,
        "minimum_area_scale": 1.5,
        "minimum_oriented_span_mm": 0.50,
        "minimum_visible_perimeter_mm": 0.75,
    },
    "A4": {
        "nib_role": "text",
        "ink_budget_field_fraction": 0.008,
        "max_objects": 24,
        "minimum_area_scale": 1.0,
        "minimum_oriented_span_mm": 0.50,
        "minimum_visible_perimeter_mm": 0.75,
    },
    "A3": {
        "nib_role": "text",
        "ink_budget_field_fraction": 0.012,
        "max_objects": 32,
        "minimum_area_scale": 0.75,
        "minimum_oriented_span_mm": 0.50,
        "minimum_visible_perimeter_mm": 0.75,
    },
}

# A race course is the subject of its own plate, not another road, so it gets a
# width above every road in the hierarchy rather than a place inside it. It is
# drawn in a general-colour ink, and the colour set only owns 0.25 and 0.40, so
# these targets are realised as parallel offsets of the 0.40 -- which is what
# makes the course the boldest mark on the sheet without inventing a nib.
#
# The cost is trivial where it matters: 42.195 km at ~1:150,000 is only ~280 mm
# of drawn line, so even four passes is under a metre of pen-down.
# Each target is strictly wider than that sheet's `heavy` road, so the course
# wins on weight as well as colour. Anything narrower than the major-road mark
# reads as one more road, which is the whole failure this is here to avoid.
RACE_COURSE = {
    "A5": {"ink": "Red", "target_width_mm": 0.80},
    "A4": {"ink": "Red", "target_width_mm": 1.20},
    "A3": {"ink": "Red", "target_width_mm": 1.60},
}

# Which sheets carry which subject. Engineered-object schematics use a binding
# size-aware LOD policy: A5 preserves identity and requested orthographic views,
# while A4/A3 progressively restore source-supported fine detail.
SUBJECT_POLICY = {
    "map": {
        "sheets": ["A5", "A4", "A3"],
        "preferred": "A4",
        "note": (
            "A5 only carries a culled feature set. Full all-roads detail is an "
            "A3 design; A4 is the practical compromise."
        ),
    },
    "schematic": {
        "sheets": ["A5", "A4", "A3"],
        "preferred": "A3",
        "note": (
            "Car/aircraft/ship line drawings use all three sheets with physical "
            "LOD: identity views survive A5; A4/A3 restore progressively finer "
            "mechanical detail. A3 remains the preferred collector edition."
        ),
    },
    "route_plate": {
        "sheets": ["A5", "A4", "A3"],
        "preferred": "A5",
        "note": (
            "A single highlighted route with a deliberately culled terrain "
            "context may use A5; dense topographic or navigational editions "
            "must move up in sheet size rather than shrink below real nibs."
        ),
    },
}

BORDER_STYLES = ("none", "hairline", "double", "rule", "corner")
DEFAULT_BORDER_STYLE = "double"

# Landscape information rail, as a fraction of the content width.
RAIL_FRACTION = 0.265

# City headers stack the name and coordinates on the left, with a separate
# compass column on the right. Landscape city plates use a full-width head.
# Compass dimensions follow the reviewed A5 reference and the derived bands.
A3_CITY_HEADER_COMPASS_HEIGHT_MULTIPLE = 1.75
A3_CITY_COMPASS_NIB_MM = 0.25
A3_CITY_COORDINATE_TRACKING_NIB_MULTIPLE = 1.0
A3_CITY_COORDINATE_SEPARATOR_SPACES = 3
A3_CITY_COMPASS_STYLE = "north-arrow-east-west-axis"
A3_CITY_COMPASS_ARROW_HEAD_HALF_WIDTH_ZONE_FRACTION = 0.14
A3_CITY_COMPASS_ARROW_HEAD_HEIGHT_DRAWABLE_FRACTION = 0.24
A3_CITY_COMPASS_EAST_WEST_HALF_WIDTH_ZONE_FRACTION = 0.22
A3_CITY_COMPASS_EAST_WEST_CROSSING_DRAWABLE_FRACTION = 0.58
STANDARD_CITY_COMPASS_NIB_MM = 0.40
STANDARD_CITY_COMPASS_STYLE = "diamond-cardinal"

# Clean personalised university memorabilia shares the generated city header.
# Its coordinates add tracking; the compass stays in the right column. Footer
# copy uses the existing memorabilia cells without labels or writing rules.
# Tracking and inset resolve from real nibs, so this alternate composition
# introduces no private paper coordinates.
CLEAN_MEMORABILIA_COORDINATE_SEPARATOR_SPACES = 3
CLEAN_MEMORABILIA_COORDINATE_TRACKING_NIB_MULTIPLE = 1.0
CLEAN_MEMORABILIA_FOOTER_INSET_NIB_MULTIPLE = 2.0
CLEAN_MEMORABILIA_COMPASS_NIB_MM = 0.40
CLEAN_MEMORABILIA_COMPASS_STYLE = "diamond-cardinal"
CLEAN_MEMORABILIA_FOOTER_NIB_MM = 0.40
CLEAN_MEMORABILIA_FOOTER_FONT_ROLE = "display-serif"
CLEAN_MEMORABILIA_NAME_CAP_MULTIPLE = 1.70
CLEAN_MEMORABILIA_SECONDARY_CAP_MULTIPLE = 1.50

DETAIL_LINES = 3

# Circuit plates use the same page archetypes as every other map, but their
# information is a set of four factual cards rather than three unrelated lines.
# The alternate composition only subdivides bands the binding plate already
# owns: in landscape it uses the otherwise open rail between subtitle and
# attribution; in portrait it uses the existing furniture/detail foot stack.
CIRCUIT_INFORMATION_CARDS = 4

# Bridge technical-plate composition and physical pen grammar.  These move the
# existing bridge renderer's paper decisions into the generated plate contract:
# the field label, structural drawing and dimension label are a named optional
# stack inside `map_field`, and every coloured/structural role resolves through
# the real studio ladder.  Bridge subjects currently select A3, but deriving the
# records for all six formats prevents a later paper-size option from inventing
# a second set of constants.
BRIDGE_FINE_NIB_MM = 0.25
BRIDGE_SECONDARY_NIB_MM = 0.40
BRIDGE_ROLE_INKS = {
    "construction": "Grey",
    "context": "Blue",
    "dimension": "Red",
    "copy": "Black",
    "fine": "Black",
    "secondary": "Black",
    "primary": "Black",
    "frame": "Black",
}

# Engineered-object line hierarchy.  The semantic names are intentionally
# independent of any one category: a principal silhouette can be a car roof,
# a yacht sheer, an aircraft planform, or a turbine casing.  Fine source detail
# stays on the smallest real nib while the identity-carrying silhouette follows
# the plate's primary furniture role.  This is a physical pen mapping, not a
# colour mandate; a renderer may expose the semantic class while this contract
# resolves the studio pen that can actually draw it.
TECHNICAL_FINE_NIB_MM = 0.25
TECHNICAL_SECONDARY_NIB_MM = 0.40
TECHNICAL_ACCENT_NIB_MM = 0.40
TECHNICAL_ROLE_INKS = {
    "principal_silhouette": "Black",
    "major_structural_edges": "Black",
    "glazing_openings": "Blue",
    "panel_seam_lines": "Black",
    "mechanical_detail": "Black",
    "internal_cutaway_structure": "Red",
    "texture_material_hatching": "Grey",
    "shadow_hatching": "Grey",
    "construction_geometry": "Grey",
    "dimensions_leaders": "Red",
    "labels_specifications": "Black",
    "accent_feature": "Purple",
    "background_context": "Grey",
}

# Personalised university-memorabilia subcomposition.  These reference
# rectangles were approved on A5 and scale with the A-series short edge.  They
# subdivide the existing title/subtitle/furniture/detail stack; critically,
# they do not move or resize the map field that defines every city crop.
MEMORABILIA_REFERENCE_SHORT_MM = 148.0
MEMORABILIA_A5_ZONES = {
    "memorabilia_city_title": (12.0, 12.0, 78.0, 12.6),
    "memorabilia_coordinates": (94.0, 12.0, 42.0, 3.0),
    "memorabilia_compass": (126.0, 17.0, 10.0, 14.0),
    "memorabilia_person_name": (12.0, 167.046, 124.0, 10.5),
    "memorabilia_degree": (12.0, 180.0, 54.0, 11.0),
    "memorabilia_honours": (70.0, 180.0, 38.0, 11.0),
    "memorabilia_years": (112.0, 180.0, 24.0, 11.0),
}


def _round(value: float) -> float:
    return round(value + 0.0, 3)


def _rect(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": _round(x), "y": _round(y), "width": _round(w), "height": _round(h)}


def _type_scale(short_edge: float) -> dict[str, float]:
    title = TITLE_RATIO * short_edge
    return {role: _round(title * ratio) for role, ratio in TYPE_ROLES.items()}


def _nibs(sheet: str) -> dict[str, float]:
    return dict(NIB_ROLES[sheet])


def _title_zone_height(cap: dict[str, float], title_nib_mm: float) -> float:
    """Return a band that can contain two minimum-cap title ink envelopes."""

    minimum_cap_mm = title_nib_mm * MIN_CAP_HEIGHT_NIB_MULTIPLE
    physical_two_line_height = (
        2.0 * minimum_cap_mm
        + title_nib_mm
        + title_nib_mm * TITLE_LINE_INK_CLEARANCE_NIB_MULTIPLE
    )
    # One additional nib contains the outer half-nib envelopes at the top and
    # bottom of the stacked block.
    physical_two_line_height += title_nib_mm
    return max(cap["title"] * 1.80, physical_two_line_height)


def _portrait_zones(
    w: float,
    h: float,
    safe: float,
    cap: dict[str, float],
    title_nib_mm: float,
) -> dict:
    """Stack archetype: title over map over details, attribution at the foot."""
    gap = safe * 0.5
    cx, cy = 2 * safe, 2 * safe
    cw, ch = w - 4 * safe, h - 4 * safe

    title_h = _title_zone_height(cap, title_nib_mm)
    sub_h = cap["subtitle"] * 2.00
    detail_leading = cap["detail"] * 2.80
    detail_h = cap["detail"] + detail_leading * (DETAIL_LINES - 1)
    furniture_h = cap["legend"] * 2.40
    attr_h = cap["attribution"] * 2.00

    used = title_h + sub_h + furniture_h + detail_h + attr_h + gap * 5
    map_h = ch - used

    y = cy
    zones = {"title": _rect(cx, y, cw, title_h)}
    y += title_h + gap
    zones["subtitle"] = _rect(cx, y, cw, sub_h)
    y += sub_h + gap
    zones["map_field"] = _rect(cx, y, cw, map_h)
    y += map_h + gap
    zones["furniture"] = _rect(cx, y, cw, furniture_h)
    y += furniture_h + gap
    zones["detail"] = _rect(cx, y, cw, detail_h)
    zones["attribution"] = _rect(cx, cy + ch - attr_h, cw, attr_h)
    return zones


# Crew composition. Its own head, because a crew plate says more than a city
# name: the race, then club/event/category, then city/river/coordinates. The
# compass moves out of that stack into a column of its own on the right, so the
# three text lines share one left edge with everything below them.
#
# Nine block rows is an eight plus cox, the largest crew, so the band is sized
# once and every smaller boat leaves the tail of it empty rather than re-flowing.
CREW_META_LINES = 2
CREW_META_LEADING = 1.55  # x detail cap
CREW_COMPASS_WIDTH = 2.00  # x title cap
CREW_BLOCK_ROWS = 9
CREW_BLOCK_BODY = 1.15  # x detail cap; the block is set slightly larger
CREW_BLOCK_LEADING = 1.50  # x block body cap
CREW_BLOCK_HEAD = 2.20  # x detail cap, for the standing head + rule
# Three decimals are appropriate for millimetre coordinates, but not for an
# aspect that is applied back across a full map field: that rounding left the
# A5 crew map about 0.015 mm narrower than its 124 mm band. Six decimals keeps
# the derived crop aligned to substantially better than 0.001 mm on every
# supported sheet while retaining a compact, deterministic contract value.
CREW_MAP_FIELD_ASPECT_DECIMALS = 6


def _crew_zones(w: float, h: float, safe: float, cap: dict[str, float]) -> dict:
    """Stack archetype: three-line head, map, then crew and result."""

    gap = safe * 0.5
    cx, cy = 2 * safe, 2 * safe
    cw, ch = w - 4 * safe, h - 4 * safe

    title_h = cap["title"] * 1.80
    meta_leading = cap["detail"] * CREW_META_LEADING
    meta_h = cap["detail"] + meta_leading * (CREW_META_LINES - 1)
    compass_w = cap["title"] * CREW_COMPASS_WIDTH
    head_w = cw - compass_w - gap

    body_cap = cap["detail"] * CREW_BLOCK_BODY
    block_leading = body_cap * CREW_BLOCK_LEADING
    block_h = (
        cap["detail"] * CREW_BLOCK_HEAD
        + body_cap
        + block_leading * (CREW_BLOCK_ROWS - 1)
    )
    attr_h = cap["attribution"] * 2.00

    used = title_h + meta_h + block_h + attr_h + gap * 3
    map_h = ch - used

    y = cy
    zones = {"crew_title": _rect(cx, y, head_w, title_h)}
    zones["crew_compass"] = _rect(
        cx + cw - compass_w, y, compass_w, title_h + gap + meta_h
    )
    y += title_h + gap
    zones["crew_meta"] = _rect(cx, y, head_w, meta_h)
    y += meta_h + gap
    zones["crew_map_field"] = _rect(cx, y, cw, map_h)
    y += map_h + gap
    zones["crew_block"] = _rect(cx, y, cw, block_h)
    return zones


def _landscape_zones(
    w: float,
    h: float,
    safe: float,
    cap: dict[str, float],
    title_nib_mm: float,
) -> dict:
    """Rail archetype: map fills the sheet, information stacks in a right rail.

    A bottom band on a wide sheet wastes a disproportionate amount of paper,
    so landscape uses a vertical rail instead of reusing the portrait stack.
    """
    gap = safe * 0.5
    cx, cy = 2 * safe, 2 * safe
    cw, ch = w - 4 * safe, h - 4 * safe

    rail_w = cw * RAIL_FRACTION
    map_w = cw - rail_w - gap
    rail_x = cx + map_w + gap

    title_h = _title_zone_height(cap, title_nib_mm)
    sub_h = cap["subtitle"] * 2.00
    detail_leading = cap["detail"] * 2.80
    detail_h = cap["detail"] + detail_leading * (DETAIL_LINES - 1)
    furniture_h = cap["legend"] * 2.40
    attr_h = cap["attribution"] * 2.00

    zones = {"map_field": _rect(cx, cy, map_w, ch)}
    y = cy
    zones["title"] = _rect(rail_x, y, rail_w, title_h)
    y += title_h + gap
    zones["subtitle"] = _rect(rail_x, y, rail_w, sub_h)
    y += sub_h + gap
    zones["detail"] = _rect(rail_x, y, rail_w, detail_h)
    y += detail_h + gap
    zones["furniture"] = _rect(rail_x, y, rail_w, furniture_h)
    zones["attribution"] = _rect(rail_x, cy + ch - attr_h, rail_w, attr_h)
    return zones


def _split_zones(zones: dict, safe: float) -> dict[str, dict[str, float]]:
    """Column-split the head and foot bands so a theme can build a side rail.

    Everything above and below the map is a full-width band. A theme that wants
    its details beside the title rather than under the map needs a narrower
    column to put them in, and it may not invent one: the only zones anything
    is allowed to draw in are the ones this file derives.

    Both splits reuse ``RAIL_FRACTION``, so a portrait rail is the same
    proportion of the content width as the landscape rail, and neither one
    moves ``map_field`` -- the crop that every city extent is projected into
    stays exactly where it was.
    """

    gap = safe * 0.5

    def columns(prefix: str, top: dict, bottom: dict) -> dict[str, dict[str, float]]:
        x = top["x"]
        y = top["y"]
        width = top["width"]
        height = bottom["y"] + bottom["height"] - y
        rail_w = width * RAIL_FRACTION
        main_w = width - rail_w - gap
        return {
            f"{prefix}_main": _rect(x, y, main_w, height),
            f"{prefix}_rail": _rect(x + main_w + gap, y, rail_w, height),
        }

    return {
        **columns("head", zones["title"], zones["subtitle"]),
        **columns("foot", zones["furniture"], zones["detail"]),
    }


def _circuit_zones(
    zones: dict[str, dict[str, float]],
    gap: float,
    orientation: str,
) -> dict[str, dict[str, float]]:
    """Derive the four-card circuit information composition.

    Landscape cards form a calm vertical rhythm in the right rail. Portrait
    cards form a two-by-two grid in the existing foot bands. In both cases the
    title, subtitle, map field and legal attribution remain untouched.
    """

    names = (
        "circuit_course",
        "circuit_history",
        "circuit_record",
        "circuit_drawing",
    )
    if len(names) != CIRCUIT_INFORMATION_CARDS:
        raise ValueError("circuit information card count drifted")

    if orientation == "landscape":
        reference = zones["detail"]
        x = reference["x"]
        y = reference["y"]
        width = reference["width"]
        bottom = zones["attribution"]["y"] - gap
        available_height = bottom - y
        card_height = (
            available_height - gap * (CIRCUIT_INFORMATION_CARDS - 1)
        ) / CIRCUIT_INFORMATION_CARDS
        if card_height <= 0:
            raise ValueError("circuit rail cannot fit four information cards")
        return {
            name: _rect(
                x,
                y + index * (card_height + gap),
                width,
                card_height,
            )
            for index, name in enumerate(names)
        }

    top = zones["furniture"]["y"]
    bottom = zones["detail"]["y"] + zones["detail"]["height"]
    x = zones["furniture"]["x"]
    width = zones["furniture"]["width"]
    card_width = (width - gap) / 2.0
    card_height = (bottom - top - gap) / 2.0
    if min(card_width, card_height) <= 0:
        raise ValueError("portrait circuit grid cannot fit four information cards")
    return {
        name: _rect(
            x + (index % 2) * (card_width + gap),
            top + (index // 2) * (card_height + gap),
            card_width,
            card_height,
        )
        for index, name in enumerate(names)
    }


def _bridge_zones(
    zones: dict[str, dict[str, float]],
    gap: float,
    cap: dict[str, float],
) -> dict[str, dict[str, float]]:
    """Derive the bridge label/drawing/dimension stack inside `map_field`."""

    field = zones["map_field"]
    inner_x = field["x"] + gap
    inner_y = field["y"] + gap
    inner_width = field["width"] - 2.0 * gap
    inner_height = field["height"] - 2.0 * gap
    label_height = cap["attribution"] + gap
    drawing_x = inner_x + gap
    drawing_y = inner_y + label_height
    drawing_width = inner_width - 2.0 * gap
    drawing_height = inner_height - 2.0 * label_height
    if min(drawing_width, drawing_height) <= 0:
        raise ValueError("bridge zones cannot fit inside the derived map field")
    return {
        "bridge_field_label": _rect(
            inner_x,
            inner_y,
            inner_width,
            label_height,
        ),
        "bridge_drawing": _rect(
            drawing_x,
            drawing_y,
            drawing_width,
            drawing_height,
        ),
        "bridge_dimension_label": _rect(
            inner_x,
            inner_y + inner_height - label_height,
            inner_width,
            label_height,
        ),
    }


def _bridge_pen_roles(
    sheet: str, nibs: dict[str, float]
) -> dict[str, dict[str, str | float]]:
    nib_by_role = {
        "construction": BRIDGE_FINE_NIB_MM,
        "context": BRIDGE_FINE_NIB_MM,
        "dimension": BRIDGE_FINE_NIB_MM,
        "copy": nibs["text"],
        "fine": BRIDGE_FINE_NIB_MM,
        "secondary": BRIDGE_SECONDARY_NIB_MM,
        "primary": nibs["primary"],
        "frame": nibs["primary"],
    }
    unavailable = sorted(set(nib_by_role.values()) - set(NIB_LADDER[sheet]))
    if unavailable:
        raise ValueError(
            f"bridge roles request unavailable {sheet} nibs: {unavailable}"
        )
    return {
        role: {"ink": BRIDGE_ROLE_INKS[role], "nib_mm": _round(nib)}
        for role, nib in nib_by_role.items()
    }


def _technical_zones(
    zones: dict[str, dict[str, float]], gap: float
) -> dict[str, dict[str, float]]:
    """Derive reusable equal-panel object compositions inside ``map_field``.

    A hero uses ``technical_field``.  Three-view, patent, workshop, owner and
    specification presets combine the equal top/bottom or left/right panels.
    The only spacing input is the plate's generated gap, so no renderer owns a
    private paper constant.
    """

    field = zones["map_field"]
    inner = _rect(
        field["x"] + gap,
        field["y"] + gap,
        field["width"] - 2.0 * gap,
        field["height"] - 2.0 * gap,
    )
    half_width = (inner["width"] - gap) / 2.0
    half_height = (inner["height"] - gap) / 2.0
    if min(half_width, half_height) <= 0:
        raise ValueError("technical zones cannot fit inside the derived map field")
    left_x = inner["x"]
    right_x = inner["x"] + half_width + gap
    top_y = inner["y"]
    bottom_y = inner["y"] + half_height + gap
    return {
        "technical_field": inner,
        "technical_top": _rect(inner["x"], top_y, inner["width"], half_height),
        "technical_bottom_left": _rect(left_x, bottom_y, half_width, half_height),
        "technical_bottom_right": _rect(right_x, bottom_y, half_width, half_height),
        "technical_left": _rect(left_x, inner["y"], half_width, inner["height"]),
        "technical_right_top": _rect(right_x, top_y, half_width, half_height),
        "technical_right_bottom": _rect(right_x, bottom_y, half_width, half_height),
    }


def _technical_pen_roles(
    sheet: str, nibs: dict[str, float]
) -> dict[str, dict[str, str | float]]:
    nib_by_role = {
        "principal_silhouette": nibs["primary"],
        "major_structural_edges": TECHNICAL_SECONDARY_NIB_MM,
        "glazing_openings": TECHNICAL_FINE_NIB_MM,
        "panel_seam_lines": TECHNICAL_FINE_NIB_MM,
        "mechanical_detail": TECHNICAL_FINE_NIB_MM,
        "internal_cutaway_structure": TECHNICAL_FINE_NIB_MM,
        "texture_material_hatching": TECHNICAL_FINE_NIB_MM,
        "shadow_hatching": TECHNICAL_FINE_NIB_MM,
        "construction_geometry": TECHNICAL_FINE_NIB_MM,
        "dimensions_leaders": TECHNICAL_FINE_NIB_MM,
        "labels_specifications": nibs["text"],
        "accent_feature": TECHNICAL_ACCENT_NIB_MM,
        "background_context": TECHNICAL_FINE_NIB_MM,
    }
    unavailable = sorted(set(nib_by_role.values()) - set(NIB_LADDER[sheet]))
    if unavailable:
        raise ValueError(
            f"technical roles request unavailable {sheet} nibs: {unavailable}"
        )
    return {
        role: {"ink": TECHNICAL_ROLE_INKS[role], "nib_mm": _round(nib)}
        for role, nib in nib_by_role.items()
    }


def _memorabilia_zones(short_edge_mm: float) -> dict[str, dict[str, float]]:
    scale = short_edge_mm / MEMORABILIA_REFERENCE_SHORT_MM
    return {
        name: _rect(x * scale, y * scale, width * scale, height * scale)
        for name, (x, y, width, height) in MEMORABILIA_A5_ZONES.items()
    }


def _memorabilia_variants(sheet: str) -> dict[str, dict]:
    """Alternate arrangements using only generated memorabilia/split zones."""

    text_nib_mm = min(NIB_LADDER[sheet])
    separator = (
        " " * CLEAN_MEMORABILIA_COORDINATE_SEPARATOR_SPACES
        + "/"
        + " " * CLEAN_MEMORABILIA_COORDINATE_SEPARATOR_SPACES
    )
    return {
        "clean-personalised": {
            "header": {
                "title_zone": "city_title",
                "title_wrap": True,
                "coordinates_zone": "city_coordinates",
                "coordinate_align_reference": "title-ink-left",
                "compass_zone": "city_compass",
                "coordinate_layout": "inline",
                "coordinate_separator": separator,
                "coordinate_tracking_mm": _round(
                    text_nib_mm
                    * CLEAN_MEMORABILIA_COORDINATE_TRACKING_NIB_MULTIPLE
                ),
                "coordinate_align": "left",
                "compass_nib_mm": CLEAN_MEMORABILIA_COMPASS_NIB_MM,
                "compass_style": CLEAN_MEMORABILIA_COMPASS_STYLE,
                "compass_geometry": {},
            },
            "footer": {
                "show_labels": False,
                "show_write_lines": False,
                "visible_fields": ["person_name", "degree", "years"],
                "field_zone_spans": {
                    "person_name": ["memorabilia_person_name"],
                    "degree": ["memorabilia_degree", "memorabilia_honours"],
                    "years": ["memorabilia_years"],
                },
                "field_align": {
                    "person_name": "left",
                    "degree": "left",
                    "years": "right",
                },
                "value_vertical": "middle",
                "value_cap_role": "detail",
                "value_font_role": CLEAN_MEMORABILIA_FOOTER_FONT_ROLE,
                "value_nib_mm": CLEAN_MEMORABILIA_FOOTER_NIB_MM,
                "name_cap_multiplier": CLEAN_MEMORABILIA_NAME_CAP_MULTIPLE,
                "secondary_cap_multiplier": (
                    CLEAN_MEMORABILIA_SECONDARY_CAP_MULTIPLE
                ),
                "horizontal_bounds_zone": "map_field",
                "value_inset_mm": _round(
                    CLEAN_MEMORABILIA_FOOTER_NIB_MM
                    * CLEAN_MEMORABILIA_FOOTER_INSET_NIB_MULTIPLE
                ),
                "honours_policy": "must-be-empty",
            },
        }
    }


def _city_zones(
    zones: dict[str, dict[str, float]],
    gap: float,
    orientation: str,
    short_edge_mm: float,
    sheet: str,
    cap: dict[str, float],
) -> dict[str, dict[str, float]]:
    """Stack title/coordinates left and reserve the right column for north.

    Portrait map fields and the reviewed A3 landscape field stay exact. The
    smaller landscape formats use the same full-width city composition.
    University exports reuse the header zones, retaining their own map/footer.
    """

    field = zones["map_field"]
    reference = _memorabilia_zones(short_edge_mm)["memorabilia_compass"]
    if orientation == "portrait":
        title = dict(zones["title"])
        coordinates = dict(zones["subtitle"])
        header_height = coordinates["y"] + coordinates["height"] - title["y"]
        compass_width = reference["width"]
        city_field = _rect(
            field["x"], field["y"], field["width"],
            zones["attribution"]["y"] - gap - field["y"],
        )
        content_width = field["width"]
    else:
        content_width = zones["title"]["x"] + zones["title"]["width"] - field["x"]
        header_height = zones["title"]["height"]
        compass_width = header_height * A3_CITY_HEADER_COMPASS_HEIGHT_MULTIPLE
        coordinate_height = cap["attribution"]
        title = _rect(
            field["x"], field["y"], content_width,
            header_height - gap - coordinate_height,
        )
        coordinates = _rect(
            field["x"], field["y"] + header_height - coordinate_height,
            content_width, coordinate_height,
        )
        map_y = field["y"] + header_height + gap
        city_field = _rect(
            field["x"], map_y, content_width,
            field["y"] + field["height"] - map_y,
        )
    text_width = content_width - compass_width - gap
    title["width"] = _round(text_width)
    coordinates["width"] = _round(text_width)
    compass = _rect(
        title["x"] + content_width - compass_width,
        title["y"], compass_width, header_height,
    )
    if min(text_width, title["height"], city_field["height"]) <= 0:
        raise ValueError("City header leaves no usable title or geographic field")
    return {
        "city_title": title,
        "city_coordinates": coordinates,
        "city_compass": compass,
        "city_map_field": city_field,
    }


def build_format(sheet: str, orientation: str) -> dict:
    short, long_ = SHEETS[sheet]
    w, h = (short, long_) if orientation == "portrait" else (long_, short)
    # Snapped to 0.5 mm so every derived coordinate stays a tidy number an
    # operator can read off the sheet: A5 6.0, A4 8.5, A3 12.0.
    safe = min(max(SAFE_RATIO * min(w, h), SAFE_MIN), SAFE_MAX)
    safe = _round(round(safe * 2.0) / 2.0)
    cap = _type_scale(min(w, h))
    nibs = _nibs(sheet)

    zones = (
        _portrait_zones(w, h, safe, cap, nibs["heavy"])
        if orientation == "portrait"
        else _landscape_zones(w, h, safe, cap, nibs["heavy"])
    )
    field = zones["map_field"]
    crew_zones = _crew_zones(w, h, safe, cap)
    crew_field = crew_zones["crew_map_field"]
    bridge_zones = _bridge_zones(zones, safe * 0.5, cap)
    technical_zones = _technical_zones(zones, safe * 0.5)
    circuit_zones = _circuit_zones(zones, safe * 0.5, orientation)
    city_zones = _city_zones(
        zones,
        safe * 0.5,
        orientation,
        min(w, h),
        sheet,
        cap,
    )
    city_field = city_zones["city_map_field"]
    a3_city_header = sheet == "A3" and orientation == "landscape"

    result = {
        "id": f"{sheet.lower()}-{orientation}",
        "sheet": sheet,
        "orientation": orientation,
        "archetype": "stack" if orientation == "portrait" else "rail",
        "page_mm": {"width": _round(w), "height": _round(h)},
        "safe_margin_mm": safe,
        "content_inset_mm": _round(2 * safe),
        "gap_mm": _round(safe * 0.5),
        "border": {
            "style": DEFAULT_BORDER_STYLE,
            "allowed_styles": list(BORDER_STYLES),
            "outer": _rect(safe, safe, w - 2 * safe, h - 2 * safe),
            "inner_offset_mm": _round(safe * 0.25),
            "nib_role": "heavy",
        },
        "zones_mm": zones,
        # Alternative composition for a personalised crew plate: the map gives
        # up height to a boat plan and a crew list. Kept out of `zones_mm`
        # because it is a different stack, not an addition to the default one;
        # a renderer opts in and takes `crew_map_field` as its map rectangle.
        "crew_zones_mm": crew_zones,
        # Optional column splits of the head and foot bands. Not part of the
        # default composition, so they are kept out of ``zones_mm`` and are not
        # required in a manifest; a theme opts in by naming one.
        "split_zones_mm": _split_zones(zones, safe),
        # Optional stack for side-elevation technical plates.  As with crew
        # zones, this is a composition selected by a subject renderer rather
        # than extra geometry in the default map layout.
        "bridge_zones_mm": bridge_zones,
        # Equal-panel subdivisions selected by the engineered-object renderer.
        # They remain outside zones_mm because they are alternate compositions,
        # not additional page bands.
        "technical_zones_mm": technical_zones,
        # Alternate factual composition for circuit studies. It subdivides
        # existing non-map bands and never changes the geographic crop.
        "circuit_zones_mm": circuit_zones,
        # City-only composition: city, coordinates and compass remain while
        # the removed personal/factual footer returns to the geographic field.
        "city_zones_mm": city_zones,
        "city_header": {
            "id": "city-header-left-stack-v1",
            "coordinate_align": "left",
            "coordinate_align_reference": "title-ink-left",
            "compass_align_reference": "header-ink-centre",
            "compass_draw_height_mm": _round(min(
                _memorabilia_zones(min(w, h))["memorabilia_compass"]["height"],
                city_zones["city_compass"]["height"] - safe * 0.5,
            )),
            "coordinate_layout": "inline",
            "coordinate_separator": (
                " " * A3_CITY_COORDINATE_SEPARATOR_SPACES
                + "/"
                + " " * A3_CITY_COORDINATE_SEPARATOR_SPACES
                if a3_city_header
                else " / "
            ),
            "coordinate_tracking_mm": _round(
                A3_CITY_COMPASS_NIB_MM
                * A3_CITY_COORDINATE_TRACKING_NIB_MULTIPLE
                if a3_city_header
                else 0.0
            ),
            "compass_nib_mm": (
                A3_CITY_COMPASS_NIB_MM
                if a3_city_header
                else STANDARD_CITY_COMPASS_NIB_MM
            ),
            "compass_style": (
                A3_CITY_COMPASS_STYLE
                if a3_city_header
                else STANDARD_CITY_COMPASS_STYLE
            ),
            "compass_geometry": (
                {
                    "arrow_head_half_width_zone_fraction": (
                        A3_CITY_COMPASS_ARROW_HEAD_HALF_WIDTH_ZONE_FRACTION
                    ),
                    "arrow_head_height_drawable_fraction": (
                        A3_CITY_COMPASS_ARROW_HEAD_HEIGHT_DRAWABLE_FRACTION
                    ),
                    "east_west_half_width_zone_fraction": (
                        A3_CITY_COMPASS_EAST_WEST_HALF_WIDTH_ZONE_FRACTION
                    ),
                    "east_west_crossing_drawable_fraction": (
                        A3_CITY_COMPASS_EAST_WEST_CROSSING_DRAWABLE_FRACTION
                    ),
                }
                if a3_city_header
                else {}
            ),
        },
        "map_field_aspect": _round(field["width"] / field["height"]),
        "city_map_field_aspect": _round(
            city_field["width"] / city_field["height"]
        ),
        "crew_map_field_aspect": round(
            crew_field["width"] / crew_field["height"],
            CREW_MAP_FIELD_ASPECT_DECIMALS,
        ),
        "type_scale_mm": cap,
        "type_nib_role": {
            "title": "heavy",
            "subtitle": "text",
            "detail": "text",
            "legend": "text",
            "attribution": "hairline",
        },
        "nib_ladder_mm": NIB_LADDER[sheet],
        "nib_roles_mm": nibs,
        "map_linework_nib_mm": dict(MAP_LINEWORK_NIBS[sheet]),
        "bridge_pen_roles": _bridge_pen_roles(sheet, nibs),
        "technical_pen_roles": _technical_pen_roles(sheet, nibs),
        "landmark_buildings": {
            **LANDMARK_BUILDINGS[sheet],
            "nib_mm": MAP_LINEWORK_NIBS[sheet][
                str(LANDMARK_BUILDINGS[sheet]["nib_role"])
            ],
            "max_ink_mm2": _round(
                LANDMARK_BUILDINGS[sheet]["ink_budget_field_fraction"]
                * field["width"]
                * field["height"]
            ),
        },
        "race_course": dict(RACE_COURSE[sheet]),
        "ink_budget": {
            "max_coverage": MAX_INK_COVERAGE,
            "field_area_mm2": _round(field["width"] * field["height"]),
            "max_ink_mm2": _round(MAX_INK_COVERAGE * field["width"] * field["height"]),
        },
        "detail_lines": DETAIL_LINES,
        "rules": {
            "min_cap_height_mm": {
                role: _round(nibs[nib_role] * MIN_CAP_HEIGHT_NIB_MULTIPLE)
                for role, nib_role in {
                    "title": "heavy",
                    "subtitle": "text",
                    "detail": "text",
                    "legend": "text",
                    "attribution": "hairline",
                }.items()
            },
            "min_stroke_mm_by_nib": {
                f"{nib:g}": _round(nib * MIN_STROKE_NIB_MULTIPLE)
                for nib in NIB_LADDER[sheet]
            },
            "title_line_layout": {
                "maximum_lines": 2,
                "nib_mm": _round(nibs["heavy"]),
                "horizontal_ink_inset_mm": _round(nibs["heavy"] / 2.0),
                "min_ink_clearance_nib_multiple": (
                    TITLE_LINE_INK_CLEARANCE_NIB_MULTIPLE
                ),
                "min_ink_clearance_mm": _round(
                    nibs["heavy"] * TITLE_LINE_INK_CLEARANCE_NIB_MULTIPLE
                ),
                "min_path_bounds_gap_mm": _round(
                    nibs["heavy"]
                    * (1.0 + TITLE_LINE_INK_CLEARANCE_NIB_MULTIPLE)
                ),
            },
        },
    }
    if orientation == "portrait":
        result["memorabilia_zones_mm"] = _memorabilia_zones(min(w, h))
        result["memorabilia_variants"] = _memorabilia_variants(sheet)
    return result


def build_spec() -> dict:
    formats = [
        build_format(sheet, orientation)
        for sheet in ("A5", "A4", "A3")
        for orientation in ("portrait", "landscape")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "id": "plate-format-v1",
        "description": (
            "Canonical plate formats for pen-plotted poster generation. "
            "All values are derived by tools/build_format_spec.py; edit the "
            "rules there, never these numbers."
        ),
        "derivation": {
            "safe_margin": f"clamp({SAFE_RATIO} * short_edge, {SAFE_MIN}, {SAFE_MAX}) mm",
            "title_cap": f"{TITLE_RATIO} * short_edge mm",
            "type_ladder_multiples_of_title": TYPE_ROLES,
            "min_cap_height": f"{MIN_CAP_HEIGHT_NIB_MULTIPLE} x nib",
            "title_line_ink_clearance": (
                f"{TITLE_LINE_INK_CLEARANCE_NIB_MULTIPLE} x title nib of white "
                "paper between serialized line ink envelopes"
            ),
            "min_stroke_length": f"{MIN_STROKE_NIB_MULTIPLE} x nib",
            "rail_fraction_landscape": RAIL_FRACTION,
            "bridge_zones": (
                "map_field inset by one gap; label bands are attribution cap + "
                "one gap; bridge_drawing receives a second horizontal gap inset"
            ),
            "bridge_pen_roles": (
                "construction/context/dimension/fine use the 0.25 mm general-colour "
                "pen; copy resolves through the sheet text role; secondary uses "
                "0.40 mm; primary and frame resolve through the sheet furniture "
                "primary role"
            ),
            "technical_zones": (
                "map_field inset by one gap, then split into equal top/bottom "
                "and left/right panels separated by the same gap"
            ),
            "technical_pen_roles": (
                "semantic engineered-object hierarchy resolved to the real "
                "studio inventory; fine roles use 0.25 mm, secondary/accent "
                "use 0.40 mm, labels use the sheet text role and the principal "
                "silhouette uses the sheet primary role"
            ),
            "city_zones": (
                "title at the left with coordinates below and a dedicated "
                "compass column at the right, centred on the actual header ink; "
                "portrait extends map_field to one gap above attribution; "
                "landscape uses a full-width map below the stacked city head"
            ),
            "clean_personalised_memorabilia": (
                "shared city_title/city_coordinates/city_compass zones with "
                "nib-derived coordinate tracking; values in the "
                "existing person_name/degree/years cells without labels or rules"
            ),
            "max_ink_coverage": MAX_INK_COVERAGE,
            "map_linework_nibs": "fine roles held at the narrowest real nib; only primary/heavy scale with the sheet",
            "landmark_buildings": (
                "weight from map_linework_nib_mm[nib_role]; inclusion from "
                "max_objects, ink_budget_field_fraction and minimum_area_scale, "
                "raised with the sheet because a bigger field can carry more "
                "named landmarks before they silt up"
            ),
        },
        "subject_policy": SUBJECT_POLICY,
        "formats": {item["id"]: item for item in formats},
    }


def serialize_spec(spec: dict) -> str:
    """Return the one deterministic representation written to every target."""

    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def write_spec(spec: dict) -> tuple[Path, ...]:
    """Write identical authoritative and installed-package copies."""

    payload = serialize_spec(spec)
    for output in SPEC_OUTPUT_PATHS:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8", newline="\n")
    return SPEC_OUTPUT_PATHS


def main() -> int:
    spec = build_spec()
    outputs = write_spec(spec)
    for output in outputs:
        print(f"wrote {output}")
    for key, fmt in spec["formats"].items():
        field = fmt["zones_mm"]["map_field"]
        print(
            f"  {key:14s} page {fmt['page_mm']['width']:>5.0f}x{fmt['page_mm']['height']:<5.0f} "
            f"safe {fmt['safe_margin_mm']:>4.1f}  map {field['width']:>6.1f}x{field['height']:<6.1f} "
            f"(aspect {fmt['map_field_aspect']:.3f})  title cap {fmt['type_scale_mm']['title']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
