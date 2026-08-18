from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from math import cos, hypot, isfinite, radians
from typing import Any

from .models import BoundingBox, EARTH_RADIUS_M, MapPlotterError, Page


PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "A5": (148.0, 210.0),
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
    "LETTER": (215.9, 279.4),
    "LEGAL": (215.9, 355.6),
}

# A poster preset names a composition and a sheet.  The value is the portrait
# plate it binds by default; ``--orientation landscape`` selects the landscape
# plate of the same sheet, which the specification composes separately.
POSTER_PRESET_FORMAT_IDS = {
    "a5-clean-poster": "a5-portrait",
    "a5-balanced-poster": "a5-portrait",
    "a4-clean-poster": "a4-portrait",
    "a4-balanced-poster": "a4-portrait",
    "a3-clean-poster": "a3-portrait",
    "a3-balanced-poster": "a3-portrait",
}
# ``clean`` is the restrained selection; ``balanced`` keeps the richer detail
# set.  Reading the composition from this table rather than from the preset
# string is what lets a second sheet reuse the same two compositions.
POSTER_PRESET_COMPOSITIONS = {
    "a5-clean-poster": "clean",
    "a5-balanced-poster": "balanced",
    "a4-clean-poster": "clean",
    "a4-balanced-poster": "balanced",
    "a3-clean-poster": "clean",
    "a3-balanced-poster": "balanced",
}
POSTER_PRESETS = frozenset(POSTER_PRESET_FORMAT_IDS)
# Kept under the original name because every call site reads it as "is this one
# of the fixed-plate poster compositions", which is now true for A4 as well.
A5_POSTER_PRESETS = POSTER_PRESETS

POSTER_ORIENTATIONS = ("portrait", "landscape")

FORMAT_SPEC_RESOURCE = "data/format-v1.json"


@lru_cache(maxsize=None)
def load_plate_format(format_id: str) -> dict[str, Any]:
    """Load one resolved plate format from the generated binding contract.

    Keeping the lookup here gives layout, extent selection, and future renderers a
    single way to consume the generated values.  The JSON remains authoritative;
    callers must never mutate the returned mapping.
    """

    resource = files("city_map_plotter").joinpath("data", "format-v1.json")
    try:
        document = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise MapPlotterError(
            f"Could not load packaged plate format contract {FORMAT_SPEC_RESOURCE}: {exc}"
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("formats"), dict):
        raise MapPlotterError(
            f"Packaged plate format contract {FORMAT_SPEC_RESOURCE} is malformed."
        )
    formats = document["formats"]
    try:
        resolved = formats[format_id]
    except KeyError as exc:
        known = sorted(formats)
        raise MapPlotterError(
            f"Unknown plate format {format_id!r}. Choose {', '.join(known)}."
        ) from exc
    if not isinstance(resolved, dict):
        raise MapPlotterError(
            f"Plate format {format_id!r} is not an object in {FORMAT_SPEC_RESOURCE}."
        )
    return resolved


def plate_format_id_for_preset(preset: str) -> str | None:
    """Return the binding plate format explicitly assigned to a preset."""

    return POSTER_PRESET_FORMAT_IDS.get(preset)


def poster_preset_composition(preset: str) -> str | None:
    """Return ``clean`` or ``balanced`` for a poster preset."""

    return POSTER_PRESET_COMPOSITIONS.get(preset)


def poster_plate_format_id(preset: str, *, orientation: str | None = None) -> str | None:
    """Resolve the plate a poster preset binds at the requested orientation.

    The sheet comes from the preset; the archetype comes from the orientation.
    Both plates are specified separately in the contract, so this only has to
    name the right one -- it must never synthesise a format id that the
    specification does not carry.
    """

    default = POSTER_PRESET_FORMAT_IDS.get(preset)
    if default is None:
        return None
    if orientation is None or orientation == "auto":
        return default
    if orientation not in POSTER_ORIENTATIONS:
        raise MapPlotterError(
            f"Poster orientation must be one of: {', '.join(POSTER_ORIENTATIONS)}."
        )
    sheet = default.split("-", 1)[0]
    return f"{sheet}-{orientation}"


def poster_sheet_name(preset: str) -> str | None:
    """Return the paper name (``A5``, ``A4``, ``A3``) a preset is bound to."""

    format_id = POSTER_PRESET_FORMAT_IDS.get(preset)
    return None if format_id is None else format_id.split("-", 1)[0].upper()


def orientation_for_extent(bbox: BoundingBox, *, sheet: str) -> str:
    """Choose the archetype whose field wastes the least paper on this extent.

    The subject decides, not a default.  A course that runs east-west along a
    waterfront belongs on the landscape rail; a loop that climbs north through a
    city belongs on the portrait stack.  Because the whole extent must be shown
    -- a marathon cannot be centre-cropped without cutting off the course --
    the extent is expanded to the field aspect, and the waste that expansion
    costs is `max(field/extent, extent/field)`.  The smaller number wins.
    """

    aspect = bbox.approximate_width_m / bbox.approximate_height_m
    if not isfinite(aspect) or aspect <= 0:
        raise MapPlotterError("Cannot choose an orientation for a degenerate extent.")
    sheet_key = sheet.strip().lower()
    best: tuple[float, str] | None = None
    for orientation in POSTER_ORIENTATIONS:
        field_aspect = float(
            load_plate_format(f"{sheet_key}-{orientation}")["map_field_aspect"]
        )
        waste = max(field_aspect / aspect, aspect / field_aspect)
        if best is None or waste < best[0]:
            best = (waste, orientation)
    assert best is not None
    return best[1]


def poster_plate_for_extent(
    bbox: BoundingBox, *, preset: str
) -> str:
    """Resolve the exact plate a preset should use for one subject's extent."""

    sheet = poster_sheet_name(preset)
    if sheet is None:
        raise MapPlotterError(f"Poster preset {preset!r} has no binding plate format.")
    orientation = orientation_for_extent(bbox, sheet=sheet)
    format_id = poster_plate_format_id(preset, orientation=orientation)
    assert format_id is not None
    return format_id


def plate_nib_ladder_mm(format_id: str | None) -> tuple[float, ...] | None:
    """Return the nib ladder a plate permits, straight from the contract."""

    if format_id is None:
        return None
    ladder = load_plate_format(format_id).get("nib_ladder_mm")
    if not isinstance(ladder, list) or not ladder:
        raise MapPlotterError(
            f"Plate format {format_id!r} does not carry a nib ladder."
        )
    widths = tuple(float(value) for value in ladder)
    if any(not isfinite(width) or width <= 0 for width in widths):
        raise MapPlotterError(
            f"Plate format {format_id!r} has a non-positive nib on its ladder."
        )
    return widths


def _rect_from_format(value: dict[str, Any], *, name: str) -> Rect:
    try:
        rect = Rect(
            float(value["x"]),
            float(value["y"]),
            float(value["width"]),
            float(value["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(f"Plate format rectangle {name!r} is invalid.") from exc
    if (
        not all(isfinite(number) for number in rect.bounds)
        or rect.width_mm <= 0
        or rect.height_mm <= 0
    ):
        raise MapPlotterError(f"Plate format rectangle {name!r} is invalid.")
    return rect


def expand_bbox_to_aspect(bbox: BoundingBox, target_aspect: float) -> BoundingBox:
    """Expand, never crop, a WGS84 extent to a physical width/height aspect."""

    if not isfinite(target_aspect) or target_aspect <= 0:
        raise MapPlotterError("Target map aspect must be greater than zero.")
    current_aspect = bbox.approximate_width_m / bbox.approximate_height_m
    latitude, longitude = bbox.center
    longitude_span = bbox.east - bbox.west
    latitude_span = bbox.north - bbox.south
    if current_aspect < target_aspect:
        longitude_span *= target_aspect / current_aspect
    else:
        latitude_span *= current_aspect / target_aspect
    try:
        return BoundingBox(
            longitude - longitude_span / 2,
            latitude - latitude_span / 2,
            longitude + longitude_span / 2,
            latitude + latitude_span / 2,
        )
    except MapPlotterError as exc:
        raise MapPlotterError(
            "The requested aspect expansion crosses the supported world bounds."
        ) from exc


def crop_bbox_to_aspect(bbox: BoundingBox, target_aspect: float) -> BoundingBox:
    """Centre-crop a WGS84 extent to a physical width/height aspect."""

    if not isfinite(target_aspect) or target_aspect <= 0:
        raise MapPlotterError("Target map aspect must be greater than zero.")
    current_aspect = bbox.approximate_width_m / bbox.approximate_height_m
    latitude, longitude = bbox.center
    longitude_span = bbox.east - bbox.west
    latitude_span = bbox.north - bbox.south
    if current_aspect < target_aspect:
        latitude_span *= current_aspect / target_aspect
    else:
        longitude_span *= target_aspect / current_aspect
    return BoundingBox(
        longitude - longitude_span / 2,
        latitude - latitude_span / 2,
        longitude + longitude_span / 2,
        latitude + latitude_span / 2,
    )


def pad_bbox(bbox: BoundingBox, fraction: float = 0.03) -> BoundingBox:
    """Expand an acquisition extent on every side by a small fraction."""

    if not isfinite(fraction) or fraction < 0:
        raise MapPlotterError("Bounding-box padding cannot be negative.")
    latitude, longitude = bbox.center
    longitude_span = (bbox.east - bbox.west) * (1 + 2 * fraction)
    latitude_span = (bbox.north - bbox.south) * (1 + 2 * fraction)
    try:
        return BoundingBox(
            longitude - longitude_span / 2,
            latitude - latitude_span / 2,
            longitude + longitude_span / 2,
            latitude + latitude_span / 2,
        )
    except MapPlotterError as exc:
        raise MapPlotterError(
            "The padded acquisition extent crosses the supported world bounds."
        ) from exc


@dataclass(frozen=True)
class Rect:
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x_mm,
            self.y_mm,
            self.x_mm + self.width_mm,
            self.y_mm + self.height_mm,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "x": round(self.x_mm, 3),
            "y": round(self.y_mm, 3),
            "width": round(self.width_mm, 3),
            "height": round(self.height_mm, 3),
        }


@dataclass(frozen=True)
class Layout:
    page: Page
    bbox: BoundingBox
    margin_mm: float
    footer_mm: float
    map_x_mm: float
    map_y_mm: float
    map_width_mm: float
    map_height_mm: float
    scale_mm_per_m: float
    preset: str = "standard"
    format_id: str | None = None
    zones: dict[str, Rect] = field(default_factory=dict)

    @property
    def scale_denominator(self) -> float:
        return 1_000 / self.scale_mm_per_m

    @property
    def clip_rect(self) -> tuple[float, float, float, float]:
        return (
            self.map_x_mm,
            self.map_y_mm,
            self.map_x_mm + self.map_width_mm,
            self.map_y_mm + self.map_height_mm,
        )

    def project_to_page(self, latitude: float, longitude: float) -> tuple[float, float]:
        center_latitude, center_longitude = self.bbox.center
        x_m = (
            EARTH_RADIUS_M
            * radians(longitude - center_longitude)
            * cos(radians(center_latitude))
        )
        y_m = EARTH_RADIUS_M * radians(latitude - center_latitude)
        half_width_m = self.bbox.approximate_width_m / 2
        half_height_m = self.bbox.approximate_height_m / 2
        x_mm = self.map_x_mm + (x_m + half_width_m) * self.scale_mm_per_m
        y_mm = self.map_y_mm + (half_height_m - y_m) * self.scale_mm_per_m
        return x_mm, y_mm


def _oriented_page(
    name: str,
    orientation: str,
    width_mm: float | None,
    height_mm: float | None,
    map_aspect: float,
    margin_mm: float,
    footer_mm: float,
) -> Page:
    if name.upper() == "CUSTOM":
        if (
            width_mm is None
            or height_mm is None
            or not isfinite(width_mm)
            or not isfinite(height_mm)
            or width_mm <= 0
            or height_mm <= 0
        ):
            raise MapPlotterError(
                "Custom paper requires positive --width-mm and --height-mm values."
            )
        base = (min(width_mm, height_mm), max(width_mm, height_mm))
    else:
        try:
            base = PAPER_SIZES_MM[name.upper()]
        except KeyError as exc:
            raise MapPlotterError(
                f"Unknown paper size {name!r}. Choose {', '.join(PAPER_SIZES_MM)} or custom."
            ) from exc

    def candidate(candidate_orientation: str) -> Page:
        width, height = (
            base if candidate_orientation == "portrait" else (base[1], base[0])
        )
        return Page(width, height, name.upper(), candidate_orientation)

    if orientation in {"portrait", "landscape"}:
        return candidate(orientation)
    if orientation != "auto":
        raise MapPlotterError("Orientation must be portrait, landscape, or auto.")

    def fitted_scale(page: Page) -> float:
        available_width = page.width_mm - 2 * margin_mm
        available_height = page.height_mm - 2 * margin_mm - footer_mm
        if available_width <= 0 or available_height <= 0:
            return -1
        return min(available_width / map_aspect, available_height)

    portrait = candidate("portrait")
    landscape = candidate("landscape")
    return landscape if fitted_scale(landscape) > fitted_scale(portrait) else portrait


def make_layout(
    bbox: BoundingBox,
    *,
    paper_name: str,
    orientation: str,
    margin_mm: float,
    footer_mm: float = 5.0,
    width_mm: float | None = None,
    height_mm: float | None = None,
) -> Layout:
    if (
        not isfinite(margin_mm)
        or not isfinite(footer_mm)
        or margin_mm < 0
        or footer_mm < 0
    ):
        raise MapPlotterError("Margins cannot be negative.")
    map_aspect = bbox.approximate_width_m / bbox.approximate_height_m
    page = _oriented_page(
        paper_name, orientation, width_mm, height_mm, map_aspect, margin_mm, footer_mm
    )
    available_width = page.width_mm - 2 * margin_mm
    available_height = page.height_mm - 2 * margin_mm - footer_mm
    if available_width <= 0 or available_height <= 0:
        raise MapPlotterError(
            "Margins and attribution footer leave no printable map area."
        )

    scale = min(
        available_width / bbox.approximate_width_m,
        available_height / bbox.approximate_height_m,
    )
    map_width = bbox.approximate_width_m * scale
    map_height = bbox.approximate_height_m * scale
    map_x = margin_mm + (available_width - map_width) / 2
    map_y = margin_mm + (available_height - map_height) / 2
    return Layout(
        page=page,
        bbox=bbox,
        margin_mm=margin_mm,
        footer_mm=footer_mm,
        map_x_mm=map_x,
        map_y_mm=map_y,
        map_width_mm=map_width,
        map_height_mm=map_height,
        scale_mm_per_m=scale,
    )


def make_poster_layout(
    bbox: BoundingBox,
    *,
    preset: str,
    format_id: str | None = None,
) -> Layout:
    """Build a poster composition from one resolved plate format.

    Canonical zone and border geometry comes directly from the generated format
    contract, so a larger sheet or the landscape rail archetype needs no new
    layout constant here.  The legacy aliases remain until the poster renderer
    has migrated to the canonical names.
    """

    if format_id is None:
        format_id = POSTER_PRESET_FORMAT_IDS.get(preset)
    if format_id is None:
        raise MapPlotterError(f"Poster preset {preset!r} has no binding plate format.")
    resolved = load_plate_format(format_id)
    try:
        page_values = resolved["page_mm"]
        page = Page(
            float(page_values["width"]),
            float(page_values["height"]),
            str(resolved["sheet"]),
            str(resolved["orientation"]),
        )
        safe_margin_mm = float(resolved["safe_margin_mm"])
        zone_values = resolved["zones_mm"]
        canonical_zones = {
            name: _rect_from_format(zone_values[name], name=f"{format_id}.{name}")
            for name in (
                "title",
                "subtitle",
                "map_field",
                "furniture",
                "detail",
                "attribution",
            )
        }
        memorabilia_zones = {
            name: _rect_from_format(value, name=f"{format_id}.{name}")
            for name, value in resolved.get("memorabilia_zones_mm", {}).items()
        }
        border = _rect_from_format(
            resolved["border"]["outer"], name=f"{format_id}.border.outer"
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Plate format {format_id!r} is missing required layout values."
        ) from exc

    map_zone = canonical_zones["map_field"]
    scale = min(
        map_zone.width_mm / bbox.approximate_width_m,
        map_zone.height_mm / bbox.approximate_height_m,
    )
    map_width = bbox.approximate_width_m * scale
    map_height = bbox.approximate_height_m * scale
    map_x = map_zone.x_mm + (map_zone.width_mm - map_width) / 2
    map_y = map_zone.y_mm + (map_zone.height_mm - map_height) / 2
    rendered_map = Rect(map_x, map_y, map_width, map_height)
    zones = dict(canonical_zones)
    zones.update(memorabilia_zones)
    zones["border"] = border
    zones["map"] = rendered_map
    zones["details"] = canonical_zones["detail"]
    zones["outer_border"] = border
    return Layout(
        page=page,
        bbox=bbox,
        margin_mm=safe_margin_mm,
        footer_mm=0.0,
        map_x_mm=map_x,
        map_y_mm=map_y,
        map_width_mm=map_width,
        map_height_mm=map_height,
        scale_mm_per_m=scale,
        preset=preset,
        format_id=format_id,
        zones=zones,
    )


def make_a5_clean_poster_layout(bbox: BoundingBox) -> Layout:
    """Build the canonical ``a5-portrait`` stack composition."""

    return make_poster_layout(bbox, preset="a5-clean-poster")


def make_a5_balanced_poster_layout(bbox: BoundingBox) -> Layout:
    """Use the same A5 composition with the richer balanced-detail renderer."""

    return make_poster_layout(bbox, preset="a5-balanced-poster")


def _perpendicular_distance(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    if start == end:
        return hypot(point[0] - start[0], point[1] - start[1])
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    return abs(
        dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0]
    ) / hypot(dx, dy)


def _simplify_open(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        start_index, end_index = pending.pop()
        max_distance = -1.0
        split_index = start_index
        for index in range(start_index + 1, end_index):
            distance = _perpendicular_distance(
                points[index], points[start_index], points[end_index]
            )
            if distance > max_distance:
                max_distance = distance
                split_index = index
        if max_distance > tolerance:
            keep.add(split_index)
            pending.append((start_index, split_index))
            pending.append((split_index, end_index))
    return [point for index, point in enumerate(points) if index in keep]


def simplify_polyline(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    if tolerance <= 0 or len(points) <= 2:
        return points
    closed = len(points) > 3 and points[0] == points[-1]
    if not closed:
        return _simplify_open(points, tolerance)

    ring = points[:-1]
    anchor = max(
        range(1, len(ring)),
        key=lambda index: hypot(
            ring[index][0] - ring[0][0], ring[index][1] - ring[0][1]
        ),
    )
    first_half = _simplify_open(ring[: anchor + 1], tolerance)
    second_half = _simplify_open(ring[anchor:] + [ring[0]], tolerance)
    simplified = first_half[:-1] + second_half
    # A closed area must retain at least three vertices plus its closing point.
    # If the tolerance is larger than the feature, keeping the original ring is
    # safer than turning a building or lake into a doubled line segment.
    return simplified if len(simplified) >= 4 else points


def _clip_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Liang-Barsky line clipping against an axis-aligned rectangle."""

    xmin, ymin, xmax, ymax = rect
    dx, dy = end[0] - start[0], end[1] - start[1]
    p = (-dx, dx, -dy, dy)
    q = (start[0] - xmin, xmax - start[0], start[1] - ymin, ymax - start[1])
    lower, upper = 0.0, 1.0
    for direction, distance in zip(p, q, strict=True):
        if direction == 0:
            if distance < 0:
                return None
            continue
        ratio = distance / direction
        if direction < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None
    return (
        (start[0] + lower * dx, start[1] + lower * dy),
        (start[0] + upper * dx, start[1] + upper * dy),
    )


def clip_polyline(
    points: list[tuple[float, float]],
    rect: tuple[float, float, float, float],
) -> list[list[tuple[float, float]]]:
    if len(points) < 2:
        return []
    result: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    epsilon = 1e-7
    for start, end in zip(points, points[1:]):
        clipped = _clip_segment(start, end, rect)
        if clipped is None:
            if len(current) >= 2:
                result.append(current)
            current = []
            continue
        clipped_start, clipped_end = clipped
        contiguous = (
            current
            and hypot(
                current[-1][0] - clipped_start[0], current[-1][1] - clipped_start[1]
            )
            <= epsilon
        )
        if not contiguous:
            if len(current) >= 2:
                result.append(current)
            current = [clipped_start]
        if (
            not current
            or hypot(current[-1][0] - clipped_end[0], current[-1][1] - clipped_end[1])
            > epsilon
        ):
            current.append(clipped_end)
    if len(current) >= 2:
        result.append(current)
    return result


def polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(
        hypot(end[0] - start[0], end[1] - start[1])
        for start, end in zip(points, points[1:])
    )
