from __future__ import annotations

import json
from dataclasses import replace
from math import isfinite
from pathlib import Path
from typing import Any

from .features import _classify_supported
from .geometry import POSTER_PRESET_FORMAT_IDS, load_plate_format
from .models import LayerStyle, MapPlotterError


# The order is also the default physical drawing order. Styles sharing a pen are
# adjacent so that the manifest does not ask for unnecessary pen swaps.
DEFAULT_STYLES: tuple[LayerStyle, ...] = (
    LayerStyle("water_areas", "Water outlines", "Blue 0.4", "#2563eb", 0.40, 10),
    LayerStyle(
        "rivers",
        "River centrelines without mapped banks",
        "Blue 0.4",
        "#2563eb",
        0.40,
        15,
    ),
    LayerStyle(
        "waterways", "Waterways and coastline", "Blue 0.25", "#2563eb", 0.25, 20
    ),
    LayerStyle(
        "green_space", "Parks and green space", "Green 0.25", "#15803d", 0.25, 30
    ),
    # A dedicated fine red pen keeps rail legible beside Grey paths. Draw it
    # after the transport network so crossings remain visible without a second
    # pass or a physically wider mark.
    LayerStyle("railways", "Railways", "Red 0.25", "#dc2626", 0.25, 120),
    LayerStyle(
        "boundaries", "Administrative boundaries", "Purple 0.25", "#7e22ce", 0.25, 50
    ),
    LayerStyle(
        "road_areas",
        "Micromapped road-surface perimeters",
        "Grey 0.25",
        "#66717d",
        0.25,
        55,
    ),
    LayerStyle("roads_major", "Major roads", "Black 0.6", "#18181b", 0.60, 60),
    LayerStyle("buildings", "Building outlines", "Black 0.25", "#18181b", 0.25, 70),
    LayerStyle("roads_secondary", "Secondary roads", "Black 0.4", "#18181b", 0.40, 80),
    LayerStyle("roads_local", "Local roads", "Black 0.25", "#18181b", 0.25, 90),
    LayerStyle("roads_other", "Other roads", "Grey 0.25", "#66717d", 0.25, 100),
    LayerStyle("paths", "Footpaths and tracks", "Grey 0.25", "#66717d", 0.25, 110),
    # The race course is the subject of its own plate, so it is drawn last and
    # heaviest. Its width comes from the plate's `race_course` block, not from
    # the road hierarchy it has to stand out from.
    LayerStyle("race_course", "Race course", "Red 0.4", "#d62828", 0.40, 130),
)

FAMILIES: dict[str, set[str]] = {
    "roads": {
        "road_areas",
        "roads_major",
        "roads_secondary",
        "roads_local",
        "roads_other",
        "paths",
    },
    "water": {"water_areas", "rivers", "waterways"},
    "railways": {"railways"},
    "parks": {"green_space"},
    "buildings": {"buildings"},
    "boundaries": {"boundaries"},
}

DEFAULT_FAMILIES = ("roads", "water", "railways", "parks")


# Semantic nib role per map layer.  The width behind each role is resolved from
# the active plate's `map_linework_nib_mm`, never written here: that table holds
# its fine end at 0.25 on every sheet and scales only `primary` and `heavy`,
# because 89% of a plate's ink length lives in the two finest roles and scaling
# them with the paper cancels the area gain.
MAP_LINEWORK_NIB_ROLES: dict[str, str] = {
    "water_areas": "primary",
    "rivers": "primary",
    "waterways": "hairline",
    "green_space": "hairline",
    "railways": "hairline",
    "boundaries": "hairline",
    "road_areas": "hairline",
    "roads_major": "heavy",
    "buildings": "hairline",
    "roads_secondary": "primary",
    "roads_local": "text",
    "roads_other": "hairline",
    "paths": "hairline",
    # Resolved from the plate's `race_course` block rather than this table;
    # listed so the course is recognised as map linework everywhere else.
    "race_course": "heavy",
}

# Poster overrides carry copy, palette and order only.  Widths arrive from the
# role table above so that the same composition on a larger sheet gets that
# sheet's pens instead of A5's.
POSTER_STYLE_OVERRIDES: dict[str, dict[str, Any]] = {
    "water_areas": {
        "label": "Broad river and water banks",
        "stroke": "#1769aa",
    },
    "rivers": {
        "label": "Rivers without mapped banks",
        "stroke": "#1769aa",
        "strokes": 1,
    },
    "waterways": {
        "label": "Streams and narrow waterways",
        "stroke": "#1769aa",
    },
    "roads_major": {
        "label": "Primary roads",
        "stroke": "#17212b",
    },
    "road_areas": {
        "label": "Micromapped street edges",
        "stroke": "#66717d",
    },
    "roads_secondary": {
        "label": "Secondary roads",
        "stroke": "#17212b",
    },
    "roads_local": {
        "label": "Local streets and service roads",
        "stroke": "#17212b",
    },
    "roads_other": {
        "label": "Service and minor roads",
        "stroke": "#66717d",
    },
    "paths": {
        "label": "Footpaths, cycleways and tracks",
        "stroke": "#66717d",
    },
    "railways": {
        "stroke": "#dc2626",
        "order": 120,
    },
    "green_space": {
        "stroke": "#287a4d",
    },
}


# Typical ground length of one segment in each road class, in metres. These are
# order-of-magnitude figures, not precision claims: an arterial runs for
# hundreds of metres between junctions, a residential street for around ninety,
# a footpath link for a few tens.
TYPICAL_SEGMENT_M: dict[str, float] = {
    "roads_major": 400.0,
    "roads_secondary": 250.0,
    "roads_local": 90.0,
    "roads_other": 60.0,
    "paths": 50.0,
    "road_areas": 40.0,
    "buildings": 40.0,
}

# A class earns its place only if a typical segment still reads as a line rather
# than a tick. One millimetre is four times the finest nib, so a segment at this
# length is still unmistakably a line; below it a class stops describing the
# city and starts filling it in, which is what makes a wide-extent plate look
# like a smear instead of a map.
MINIMUM_LEGIBLE_SEGMENT_MM = 1.0


def scale_adaptive_layers(
    scale_denominator: float,
    *,
    minimum_segment_mm: float = MINIMUM_LEGIBLE_SEGMENT_MM,
) -> dict[str, bool]:
    """Decide which road classes are worth drawing at one plate's scale.

    The same city at 1:40,000 and 1:200,000 is not the same map. Rather than
    pick a detail level per city by eye, this asks one question per class: at
    this scale, is a typical segment long enough to read? A residential street
    is 2.3 mm at 1:40,000 and 0.45 mm at 1:200,000 -- the first is a street,
    the second is noise the pen cannot resolve anyway.
    """

    if not isfinite(scale_denominator) or scale_denominator <= 0:
        raise MapPlotterError("Scale denominator must be a positive number.")
    decisions: dict[str, bool] = {}
    for layer, segment_m in TYPICAL_SEGMENT_M.items():
        drawn_mm = segment_m * 1000.0 / scale_denominator
        decisions[layer] = drawn_mm + 1e-9 >= minimum_segment_mm
    return decisions


def race_course_target_mm(format_id: str) -> float:
    """The width the plate wants a race course drawn at.

    This sits outside `map_linework_nib_mm` on purpose. The four linework roles
    describe a *road hierarchy*; the course is the subject the hierarchy is
    background to, so it is specified independently and is allowed to be the
    boldest mark on the sheet.
    """

    block = load_plate_format(format_id).get("race_course")
    if not isinstance(block, dict) or "target_width_mm" not in block:
        raise MapPlotterError(
            f"Plate format {format_id!r} does not define a race-course width."
        )
    width = float(block["target_width_mm"])
    if not isfinite(width) or width <= 0:
        raise MapPlotterError(
            f"Plate format {format_id!r} has a non-positive race-course width."
        )
    return width


def race_course_ink(format_id: str) -> str:
    block = load_plate_format(format_id).get("race_course") or {}
    ink = str(block.get("ink", "")).strip()
    if not ink:
        raise MapPlotterError(
            f"Plate format {format_id!r} does not name a race-course ink."
        )
    return ink


def map_linework_nib_role(format_id: str, layer_id: str) -> str:
    """Resolve one map layer's semantic nib role on the active plate.

    Buildings are the one layer whose role the plate may override: they are a
    capped, named-object feature with their own per-sheet policy, so what reads
    well on a large sheet is deliberately not what a small one gets.
    """

    try:
        role = MAP_LINEWORK_NIB_ROLES[layer_id]
    except KeyError as exc:
        raise MapPlotterError(
            f"Map layer {layer_id!r} has no semantic linework nib role."
        ) from exc
    if layer_id == "buildings":
        block = load_plate_format(format_id).get("landmark_buildings")
        if isinstance(block, dict) and block.get("nib_role"):
            role = str(block["nib_role"])
    return role


def map_linework_nib_mm(format_id: str, layer_id: str) -> float:
    """Resolve one map layer's target width from the active plate contract."""

    if layer_id == "race_course":
        return race_course_target_mm(format_id)
    role = map_linework_nib_role(format_id, layer_id)
    table = load_plate_format(format_id).get("map_linework_nib_mm")
    if not isinstance(table, dict) or role not in table:
        raise MapPlotterError(
            f"Plate format {format_id!r} does not define map linework role {role!r}."
        )
    width = float(table[role])
    if not isfinite(width) or width <= 0:
        raise MapPlotterError(
            f"Plate format {format_id!r} map linework role {role!r} is not a "
            "positive width."
        )
    return width


def poster_linework_styles(
    styles: dict[str, LayerStyle], format_id: str
) -> dict[str, LayerStyle]:
    """Re-target every map layer at the active plate's linework role width.

    The ink stays with the layer; only the requested width moves with the
    sheet.  A width that no pen of that ink owns is not resolved here -- it
    stays a target and `pens.fit_pen_width` turns it into real nibs, using
    parallel offsets when the ink has nothing that broad.
    """

    resolved = dict(styles)
    for layer_id, style in styles.items():
        if layer_id not in MAP_LINEWORK_NIB_ROLES:
            continue
        nib_mm = map_linework_nib_mm(format_id, layer_id)
        ink = style.ink or LayerStyle._ink_from_pen_label(style.pen)
        resolved[layer_id] = replace(
            style,
            pen=f"{ink} {nib_mm:g}",
            ink=ink,
            nib_mm=nib_mm,
            stroke_width_mm=nib_mm,
        )
    return resolved


def parse_families(value: str) -> tuple[str, ...]:
    requested = tuple(
        dict.fromkeys(part.strip().lower() for part in value.split(",") if part.strip())
    )
    unknown = sorted(set(requested) - set(FAMILIES))
    if unknown:
        raise MapPlotterError(
            f"Unknown layer family: {', '.join(unknown)}. "
            f"Choose from: {', '.join(FAMILIES)}."
        )
    if not requested:
        raise MapPlotterError("At least one layer family is required.")
    return requested


def enabled_layer_ids(families: tuple[str, ...]) -> set[str]:
    return set().union(*(FAMILIES[family] for family in families))


def _read_style_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MapPlotterError(f"Could not read style file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Style file {path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("layers", {}), dict):
        raise MapPlotterError("A style file must be an object with a 'layers' object.")
    return value


def load_styles(
    path: Path | None,
    selected_layers: set[str],
    *,
    preset: str = "standard",
    format_id: str | None = None,
) -> list[LayerStyle]:
    styles = {style.id: style for style in DEFAULT_STYLES}
    if preset in POSTER_PRESET_FORMAT_IDS:
        for layer_id, values in POSTER_STYLE_OVERRIDES.items():
            normalized = dict(values)
            if "stroke_width_mm" in normalized:
                normalized["nib_mm"] = normalized["stroke_width_mm"]
            if "pen" in normalized:
                normalized["ink"] = LayerStyle._ink_from_pen_label(normalized["pen"])
            styles[layer_id] = replace(styles[layer_id], **normalized)
        # Widths come from the plate actually being drawn, so an A4 poster is
        # never handed the A5 linework ladder.
        styles = poster_linework_styles(
            styles, format_id or POSTER_PRESET_FORMAT_IDS[preset]
        )
    if path is not None:
        overrides = _read_style_file(path).get("layers", {})
        unknown = sorted(set(overrides) - set(styles))
        if unknown:
            raise MapPlotterError(
                f"Style file contains unknown layers: {', '.join(unknown)}"
            )
        allowed = {
            "label",
            "pen",
            "stroke",
            "stroke_width_mm",
            "order",
            "enabled",
            "ink",
            "nib_mm",
            "strokes",
            "passes",
        }
        for layer_id, values in overrides.items():
            if not isinstance(values, dict):
                raise MapPlotterError(f"Style for {layer_id} must be an object.")
            unexpected = set(values) - allowed
            if unexpected:
                raise MapPlotterError(
                    f"Style for {layer_id} has unsupported fields: {', '.join(sorted(unexpected))}"
                )
            for field_name in ("label", "pen", "stroke", "ink"):
                if field_name in values and (
                    not isinstance(values[field_name], str)
                    or not values[field_name].strip()
                ):
                    raise MapPlotterError(
                        f"Style field {layer_id}.{field_name} must be non-empty text."
                    )
            for field_name in ("stroke_width_mm", "nib_mm"):
                if field_name in values and (
                    isinstance(values[field_name], bool)
                    or not isinstance(values[field_name], (int, float))
                    or not isfinite(values[field_name])
                    or values[field_name] <= 0
                ):
                    raise MapPlotterError(
                        f"Style field {layer_id}.{field_name} must be a positive number."
                    )
            if (
                "stroke_width_mm" in values
                and "nib_mm" in values
                and float(values["stroke_width_mm"]) != float(values["nib_mm"])
            ):
                raise MapPlotterError(
                    f"Style fields {layer_id}.stroke_width_mm and {layer_id}.nib_mm "
                    "must match when both are provided."
                )
            for field_name, maximum in (("strokes", 6), ("passes", 4)):
                if field_name in values and (
                    isinstance(values[field_name], bool)
                    or not isinstance(values[field_name], int)
                    or not 1 <= values[field_name] <= maximum
                ):
                    raise MapPlotterError(
                        f"Style field {layer_id}.{field_name} must be an integer "
                        f"between 1 and {maximum}."
                    )
            if "order" in values and (
                isinstance(values["order"], bool)
                or not isinstance(values["order"], int)
            ):
                raise MapPlotterError(
                    f"Style field {layer_id}.order must be an integer."
                )
            if "enabled" in values and not isinstance(values["enabled"], bool):
                raise MapPlotterError(
                    f"Style field {layer_id}.enabled must be true or false."
                )
            normalized = dict(values)
            if "nib_mm" in normalized:
                normalized["stroke_width_mm"] = normalized["nib_mm"]
            elif "stroke_width_mm" in normalized:
                normalized["nib_mm"] = normalized["stroke_width_mm"]
            if "pen" in normalized:
                if "ink" not in normalized:
                    normalized["ink"] = LayerStyle._ink_from_pen_label(
                        normalized["pen"]
                    )
                if "nib_mm" not in normalized:
                    inferred_nib = LayerStyle._nib_from_pen_label(normalized["pen"])
                    if inferred_nib is not None:
                        normalized["nib_mm"] = inferred_nib
                        normalized["stroke_width_mm"] = inferred_nib
            try:
                styles[layer_id] = replace(styles[layer_id], **normalized)
            except TypeError as exc:
                raise MapPlotterError(f"Invalid style for {layer_id}: {exc}") from exc

    result = [
        style
        for style in styles.values()
        if style.id in selected_layers and style.enabled
    ]
    for style in result:
        if style.nib_mm is None or not isfinite(style.nib_mm) or style.nib_mm <= 0:
            raise MapPlotterError(
                f"Nib width for {style.id} must be greater than zero."
            )
    return sorted(result, key=lambda item: (item.order, item.id))


def classify(tags: dict[str, str]) -> str | None:
    """Map OSM tags using the canonical JSON/PBF feature classifier."""

    return _classify_supported(tags)
