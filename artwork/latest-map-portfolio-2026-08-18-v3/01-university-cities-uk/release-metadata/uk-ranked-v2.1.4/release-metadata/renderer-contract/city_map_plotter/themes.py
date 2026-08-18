"""Hand-authorable, versioned design contracts for plotter editions.

A theme is the file you edit to change how a plate looks. It decides, for every
map layer, which **ink** draws it, at which **nib role**, and whether it draws at
all; and for every piece of lettering, which **font role** sets it, how big
relative to the plate's type scale, how it aligns, and -- the part that actually
moves the design around -- **which named zone it occupies**.

Two rules keep a theme honest, and both are enforced here rather than discovered
on paper:

* **Themes name roles, never millimetres.** ``format-v1.json`` owns every
  dimension. A theme asks for "the detail size, 1.4x" and the plate answers in
  millimetres for A5, A4 and A3 alike. The packaged contract is checked for
  stray ``*_mm`` keys by the test suite.
* **A theme may only ask for ink it owns.** Every (ink, nib role) pair is
  resolved against the real studio inventory at load time, so "Red at 0.6" fails
  with the list of Red pens that exist instead of producing a plate nobody can
  plot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Sequence

from .catalog import CatalogSubject
from .furniture import (
    ALIGNMENTS,
    BORDER_STYLES,
    NORTH_MARK_PLACEMENTS,
    ROLE_PHYSICAL_LAYERS,
    VERTICAL_PLACEMENTS,
)
from .geometry import Layout, load_plate_format
from .models import LayerStyle, MapPlotterError
from .pens import (
    ACTUAL_PEN_INVENTORY,
    MAX_PARALLEL_STROKES,
    PenInventory,
    fit_pen_width,
)
from .stroke_font import (
    STROKE_FONT_ID,
    STROKE_FONT_SHA256,
    TEXT_NORMALISATION_POLICY_ID,
    stroke_font_contract,
)
from .styles import DEFAULT_STYLES, enabled_layer_ids, parse_families
from .textweight import weighted_mark_width_mm


THEME_SCHEMA_VERSION = 2
THEME_CATALOG_ID = "city-map-plotter-themes-v2"
THEME_RESOURCE = "data/themes-v1.json"
THEME_OPTION = "--theme"
THEME_FILE_OPTION = "--theme-file"
THEME_POLICY_IDS = {
    "typography": "city-memorabilia-type-v1",
    "copy": "city-memorabilia-copy-v1",
    "placement_policy_id": "plate-format-named-zones-v1",
    "source_policy_id": "selected-family-lineage-relative-to-source-v1",
    "validation_policy_id": "format-physical-source-and-cohort-v1",
}

TYPE_ROLES = ("title", "subtitle", "detail", "legend", "attribution")
FURNITURE_LAYERS = (
    "poster_border",
    "frame",
    "map_furniture",
    "poster_title",
    "poster_subtitle",
    "poster_details",
    "attribution",
)
#: Furniture layers that exist to carry one typography role's pen.
TEXT_FURNITURE_LAYERS = {
    physical: role for role, physical in ROLE_PHYSICAL_LAYERS.items()
}

#: Cap heights are authored as a multiple of a named entry in the plate's type
#: scale. The bounds stop a theme quietly turning a subtitle into a headline.
MIN_CAP_SCALE = 0.5
MAX_CAP_SCALE = 3.0
#: A gutter is authored in units of the plate's own ``gap_mm``.
MAX_GUTTER_GAPS = 6.0

#: Options a theme owns outright: setting them on the command line would make
#: two plates that claim the same edition look different.
REQUIRED_LOCKED_OPTIONS = frozenset(
    {
        "--preset",
        "--radius-km",
        "--layers",
        "--detail-profile",
        "--simplify-mm",
        "--road-style",
        "--extent-fit",
        "--pen-profile",
        "--stock-tone",
        "--attribution-mode",
        "--external-attribution-placement",
        "--scale-bar",
        "--no-scale-bar",
        "--scale-detail",
        "--no-scale-detail",
        "--optimise",
        "--optimize",
        "--no-optimise",
        "--no-optimize",
        "--physical-audit",
        "--no-physical-audit",
        "--style",
        "--title",
        "--subtitle",
        "--detail",
        "--paper",
        "--orientation",
        "--width-mm",
        "--height-mm",
        "--margin-mm",
        "--nib-mm",
        "--png-dpi",
        "--allow-repeat-passes",
    }
)


def _stable_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _manifest_mm(value: float) -> float:
    """Use the renderer/manifest's binding calibrated-width precision."""

    return round(float(value), 6)


def _object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MapPlotterError(f"Theme field {field} must be an object.")
    return value


def _text(value: Any, *, field: str) -> str:
    result = value.strip() if isinstance(value, str) else ""
    if not result:
        raise MapPlotterError(f"Theme field {field} must be non-empty text.")
    return result


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MapPlotterError(f"Theme field {field} must be an array.")
    result = tuple(_text(item, field=f"{field}[]") for item in value)
    if not result:
        raise MapPlotterError(f"Theme field {field} cannot be empty.")
    return result


def _flag(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise MapPlotterError(f"Theme field {field} must be true or false.")
    return value


def _number(value: Any, *, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MapPlotterError(f"Theme field {field} must be a number.")
    number = float(value)
    if not low - 1e-9 <= number <= high + 1e-9:
        raise MapPlotterError(
            f"Theme field {field} is {number:g}; it must be between "
            f"{low:g} and {high:g}."
        )
    return number


def _choice(value: Any, choices: Iterable[str], *, field: str) -> str:
    options = tuple(choices)
    result = value if isinstance(value, str) else ""
    if result not in options:
        raise MapPlotterError(
            f"Theme field {field} is {value!r}; choose one of: "
            f"{', '.join(sorted(options))}."
        )
    return result


def _closed(
    record: dict[str, Any],
    expected: set[str],
    *,
    field: str,
    optional: set[str] | None = None,
) -> None:
    allowed = expected | (optional or set())
    missing = expected - set(record)
    unexpected = set(record) - allowed
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unexpected:
            details.append(f"unsupported {', '.join(sorted(unexpected))}")
        raise MapPlotterError(
            f"Theme field {field} is not closed: {'; '.join(details)}."
        )


def _resource_bytes(name: str) -> bytes:
    resource = files("city_map_plotter").joinpath("data", name)
    try:
        return resource.read_bytes()
    except OSError as exc:
        raise MapPlotterError(
            f"Could not read packaged theme dependency data/{name}: {exc}"
        ) from exc


def plate_zones(plate: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Every zone a theme may name: the default stack plus its column splits."""

    zones = dict(plate["zones_mm"])
    zones.update(plate.get("split_zones_mm", {}))
    zones.update(plate.get("memorabilia_zones_mm", {}))
    return zones


def ink_preview_color(ink: str, inventory: PenInventory | None = None) -> str:
    """The colour this ink actually is, taken from the pen inventory.

    A theme names inks, never hex. That is the only way a preview can promise
    that everything on screen is a pen somebody owns.
    """

    resolved = inventory or ACTUAL_PEN_INVENTORY
    for pen in resolved.pens:
        if pen.ink.casefold() == ink.casefold() and pen.preview_color:
            return pen.preview_color
    inks = ", ".join(sorted({pen.ink for pen in resolved.pens}))
    raise MapPlotterError(
        f"Ink {ink!r} is not in pen inventory {resolved.id!r}. Owned inks: {inks}."
    )


def _require_owned_pen(
    *,
    theme_id: str,
    what: str,
    ink: str,
    nib_role: str,
    nib_mm: float,
    inventory: PenInventory,
) -> None:
    """Refuse an ink that has no pen at the nib the design needs."""

    candidates = [
        pen for pen in inventory.pens if pen.ink.casefold() == ink.casefold()
    ]
    if not candidates:
        inks = ", ".join(sorted({pen.ink for pen in inventory.pens}))
        raise MapPlotterError(
            f"Theme {theme_id!r} {what} asks for {ink} ink, which inventory "
            f"{inventory.id!r} does not stock. Owned inks: {inks}."
        )
    if not any(abs(pen.nominal_nib_mm - nib_mm) < 1e-9 for pen in candidates):
        owned = ", ".join(
            f"{pen.nominal_nib_mm:g}"
            for pen in sorted(candidates, key=lambda pen: pen.nominal_nib_mm)
        )
        raise MapPlotterError(
            f"Theme {theme_id!r} {what} asks for {ink} at the {nib_role!r} nib "
            f"({nib_mm:g} mm), but inventory {inventory.id!r} has {ink} only at "
            f"{owned} mm. Choose another nib role or another ink."
        )


def _rects_overlap(
    left: dict[str, float], right: dict[str, float], *, tolerance: float = 0.01
) -> bool:
    return (
        left["x"] + left["width"] > right["x"] + tolerance
        and right["x"] + right["width"] > left["x"] + tolerance
        and left["y"] + left["height"] > right["y"] + tolerance
        and right["y"] + right["height"] > left["y"] + tolerance
    )


@dataclass(frozen=True)
class SeriesTheme:
    id: str
    version: int
    label: str
    description: str
    format: dict[str, Any]
    export: dict[str, Any]
    map_layers: tuple[dict[str, Any], ...]
    furniture: dict[str, Any]
    typography: dict[str, Any]
    copy: dict[str, Any]
    placement_policy_id: str
    source_policy_id: str
    validation_policy_id: str
    batch: dict[str, Any]
    sha256: str
    catalog_sha256: str
    source: str = "packaged"

    @property
    def canonical_export_args(self) -> tuple[str, ...]:
        return tuple(str(value) for value in self.export["canonical_args"])

    @property
    def locked_options(self) -> frozenset[str]:
        return frozenset(str(value) for value in self.export["locked_options"])

    @property
    def format_id(self) -> str:
        return str(self.format["format_id"])

    @property
    def drawn_layer_ids(self) -> frozenset[str]:
        return frozenset(
            str(record["id"]) for record in self.map_layers if record["draws"]
        )

    def map_layer(self, layer_id: str) -> dict[str, Any]:
        for record in self.map_layers:
            if record["id"] == layer_id:
                return record
        raise MapPlotterError(
            f"Theme {self.id!r} does not define map layer {layer_id!r}."
        )


def _validate_export(raw: Any, *, theme_id: str) -> dict[str, Any]:
    export = _object(raw, field=f"{theme_id}.export")
    _closed(export, {"canonical_args", "locked_options"}, field=f"{theme_id}.export")
    canonical_args = _string_list(
        export["canonical_args"], field=f"{theme_id}.export.canonical_args"
    )
    locked_options = frozenset(
        _string_list(
            export["locked_options"], field=f"{theme_id}.export.locked_options"
        )
    )
    if not REQUIRED_LOCKED_OPTIONS <= locked_options:
        missing = ", ".join(sorted(REQUIRED_LOCKED_OPTIONS - locked_options))
        raise MapPlotterError(
            f"Theme {theme_id!r} is missing binding option lock(s): {missing}."
        )
    canonical_options = {
        value.split("=", maxsplit=1)[0]
        for value in canonical_args
        if value.startswith("-")
    }
    if not canonical_options <= locked_options:
        missing = ", ".join(sorted(canonical_options - locked_options))
        raise MapPlotterError(
            f"Theme {theme_id!r} canonical options are not locked: {missing}."
        )
    if THEME_OPTION in canonical_options:
        raise MapPlotterError(
            f"Theme {theme_id!r} cannot name {THEME_OPTION} in its own canonical "
            "arguments."
        )
    return dict(export)


def _canonical_option_value(
    canonical_args: Sequence[str], option: str
) -> str | None:
    for index, token in enumerate(canonical_args):
        if token == option and index + 1 < len(canonical_args):
            return canonical_args[index + 1]
        if token.startswith(option + "="):
            return token.split("=", maxsplit=1)[1]
    return None


def _validate_map_layers(
    raw: Any,
    *,
    theme_id: str,
    plate: dict[str, Any],
    inventory: PenInventory,
    canonical_args: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or not raw:
        raise MapPlotterError(
            f"Theme {theme_id!r} map_layers must be a non-empty array."
        )
    known_styles = {style.id for style in DEFAULT_STYLES}
    role_widths = plate["map_linework_nib_mm"]
    records: list[dict[str, Any]] = []
    orders: list[int] = []
    for index, item in enumerate(raw):
        field = f"{theme_id}.map_layers[{index}]"
        layer = _object(item, field=field)
        _closed(
            layer,
            {"id", "ink", "nib_role", "order"},
            optional={"draws"},
            field=field,
        )
        layer_id = _text(layer["id"], field=f"{field}.id")
        if layer_id not in known_styles:
            raise MapPlotterError(
                f"Theme {theme_id!r} has unknown map layer {layer_id!r}. Known "
                f"layers: {', '.join(sorted(known_styles))}."
            )
        nib_role = _text(layer["nib_role"], field=f"{field}.nib_role")
        if nib_role not in role_widths:
            raise MapPlotterError(
                f"Theme {theme_id!r} layer {layer_id!r} uses unknown map nib role "
                f"{nib_role!r}. Known roles: {', '.join(sorted(role_widths))}."
            )
        order = layer["order"]
        if isinstance(order, bool) or not isinstance(order, int):
            raise MapPlotterError(f"Theme layer {layer_id!r} order must be an integer.")
        ink = _text(layer["ink"], field=f"{field}.ink")
        draws = _flag(layer.get("draws", True), field=f"{field}.draws")
        if draws:
            _require_owned_pen(
                theme_id=theme_id,
                what=f"map layer {layer_id!r}",
                ink=ink,
                nib_role=nib_role,
                nib_mm=float(role_widths[nib_role]),
                inventory=inventory,
            )
        orders.append(order)
        records.append(
            {
                "id": layer_id,
                "ink": ink,
                "nib_role": nib_role,
                "order": order,
                "draws": draws,
                "preview_color": ink_preview_color(ink, inventory),
            }
        )
    layer_ids = [str(record["id"]) for record in records]
    if len(layer_ids) != len(set(layer_ids)):
        raise MapPlotterError(f"Theme {theme_id!r} repeats a map layer.")
    if orders != sorted(orders) or len(orders) != len(set(orders)):
        raise MapPlotterError(
            f"Theme {theme_id!r} map layer order must be unique and ascending."
        )
    families_value = _canonical_option_value(canonical_args, "--layers")
    if families_value is None:
        raise MapPlotterError(
            f"Theme {theme_id!r} canonical export arguments must select --layers."
        )
    selected = enabled_layer_ids(parse_families(families_value))
    if set(layer_ids) != selected:
        undefined = ", ".join(sorted(selected - set(layer_ids)))
        surplus = ", ".join(sorted(set(layer_ids) - selected))
        detail = "; ".join(
            part
            for part in (
                f"missing {undefined}" if undefined else "",
                f"undeclared by --layers {families_value!r}: {surplus}"
                if surplus
                else "",
            )
            if part
        )
        raise MapPlotterError(
            f"Theme {theme_id!r} map_layers must cover exactly the layers its "
            f"canonical export arguments select: {detail}."
        )
    return tuple(records)


def _validate_furniture(
    raw: Any,
    *,
    theme_id: str,
    plate: dict[str, Any],
    inventory: PenInventory,
) -> dict[str, Any]:
    furniture = _object(raw, field=f"{theme_id}.furniture")
    _closed(furniture, set(FURNITURE_LAYERS), field=f"{theme_id}.furniture")
    nib_roles = plate["nib_roles_mm"]
    resolved: dict[str, Any] = {}
    for layer_id in FURNITURE_LAYERS:
        field = f"{theme_id}.furniture.{layer_id}"
        record = _object(furniture[layer_id], field=field)
        extras: set[str] = set()
        if layer_id == "poster_border":
            extras.add("style")
        if layer_id == "map_furniture":
            extras |= {"north_mark", "scale_bar"}
        if layer_id in TEXT_FURNITURE_LAYERS:
            extras.add("weight")
        _closed(
            record,
            {"draws", "ink", "nib_role"},
            optional=extras,
            field=field,
        )
        ink = _text(record["ink"], field=f"{field}.ink")
        nib_role = _text(record["nib_role"], field=f"{field}.nib_role")
        if nib_role not in nib_roles:
            raise MapPlotterError(
                f"Theme {theme_id!r} furniture {layer_id!r} uses unknown nib role "
                f"{nib_role!r}. Known roles: {', '.join(sorted(nib_roles))}."
            )
        draws = _flag(record["draws"], field=f"{field}.draws")
        if draws:
            _require_owned_pen(
                theme_id=theme_id,
                what=f"furniture {layer_id!r}",
                ink=ink,
                nib_role=nib_role,
                nib_mm=float(nib_roles[nib_role]),
                inventory=inventory,
            )
        entry: dict[str, Any] = {
            "draws": draws,
            "ink": ink,
            "nib_role": nib_role,
            "preview_color": ink_preview_color(ink, inventory),
            "weight": 1,
        }
        if layer_id in TEXT_FURNITURE_LAYERS:
            entry["weight"] = int(
                _number(
                    record.get("weight", 1),
                    field=f"{field}.weight",
                    low=1,
                    high=MAX_PARALLEL_STROKES,
                )
            )
        if layer_id == "poster_border":
            entry["style"] = _choice(
                record.get("style", "double"),
                BORDER_STYLES,
                field=f"{field}.style",
            )
        if layer_id == "map_furniture":
            entry["north_mark"] = _choice(
                record.get("north_mark", "field-north-east"),
                NORTH_MARK_PLACEMENTS,
                field=f"{field}.north_mark",
            )
            entry["scale_bar"] = _flag(
                record.get("scale_bar", False), field=f"{field}.scale_bar"
            )
        resolved[layer_id] = entry
    return resolved


def _validate_typography(
    raw: Any,
    *,
    theme_id: str,
    plate: dict[str, Any],
    furniture: dict[str, Any],
    inventory: PenInventory,
) -> dict[str, Any]:
    typography = _object(raw, field=f"{theme_id}.typography")
    _closed(
        typography,
        {"policy_id", "font_roles", "normalisation_policy_id", "roles"},
        field=f"{theme_id}.typography",
    )
    if typography["policy_id"] != THEME_POLICY_IDS["typography"]:
        raise MapPlotterError(
            f"Theme {theme_id!r} references an unsupported typography policy."
        )
    font_roles = _object(
        typography["font_roles"], field=f"{theme_id}.typography.font_roles"
    )
    _closed(
        font_roles,
        {"display", "text", "mono"},
        field=f"{theme_id}.typography.font_roles",
    )
    if set(font_roles.values()) != {STROKE_FONT_ID}:
        raise MapPlotterError(
            f"Theme {theme_id!r} references a font other than the installed "
            f"{STROKE_FONT_ID!r}."
        )
    if typography["normalisation_policy_id"] != TEXT_NORMALISATION_POLICY_ID:
        raise MapPlotterError(
            f"Theme {theme_id!r} references an unavailable text normalisation policy."
        )
    roles = _object(typography["roles"], field=f"{theme_id}.typography.roles")
    if set(roles) != set(TYPE_ROLES):
        raise MapPlotterError(
            f"Theme {theme_id!r} must set exactly these typography roles: "
            f"{', '.join(TYPE_ROLES)}."
        )

    zones = plate_zones(plate)
    type_scale = plate["type_scale_mm"]
    gap_mm = float(plate["gap_mm"])
    resolved: dict[str, Any] = {}
    claimed: dict[str, str] = {}
    for role in TYPE_ROLES:
        field = f"{theme_id}.typography.roles.{role}"
        record = _object(roles[role], field=field)
        _closed(
            record,
            {"font_role", "zone", "align", "vertical", "case", "max_lines"},
            optional={"cap_role", "cap_scale", "gutter"},
            field=field,
        )
        font_role = _choice(
            record["font_role"], font_roles, field=f"{field}.font_role"
        )
        zone = _text(record["zone"], field=f"{field}.zone")
        if zone not in zones:
            raise MapPlotterError(
                f"Theme {theme_id!r} typography role {role!r} names zone {zone!r}, "
                f"which plate {plate['id']} does not define. Available zones: "
                f"{', '.join(sorted(zones))}."
            )
        align = _choice(record["align"], ALIGNMENTS, field=f"{field}.align")
        if align == "split" and role != "attribution":
            raise MapPlotterError(
                f"Theme {theme_id!r} typography role {role!r} cannot use the "
                "'split' alignment; only attribution has two parts to split."
            )
        vertical = _choice(
            record["vertical"], VERTICAL_PLACEMENTS, field=f"{field}.vertical"
        )
        case = _choice(record["case"], ("upper", "mixed"), field=f"{field}.case")
        max_lines = int(
            _number(record["max_lines"], field=f"{field}.max_lines", low=1, high=6)
        )
        cap_role = _choice(
            record.get("cap_role", role), type_scale, field=f"{field}.cap_role"
        )
        cap_scale = _number(
            record.get("cap_scale", 1.0),
            field=f"{field}.cap_scale",
            low=MIN_CAP_SCALE,
            high=MAX_CAP_SCALE,
        )
        gutter = _number(
            record.get("gutter", 0.0), field=f"{field}.gutter", low=0.0,
            high=MAX_GUTTER_GAPS,
        )

        # The physical mark that will draw this role decides its legibility
        # floor, so cap height cannot be validated before the pen is known.
        physical_layer_id = ROLE_PHYSICAL_LAYERS[role]
        physical = furniture[physical_layer_id]
        nib_mm = float(plate["nib_roles_mm"][physical["nib_role"]])
        cap_mm = float(type_scale[cap_role]) * cap_scale
        floor_mm = 8.0 * nib_mm
        if physical["draws"] and cap_mm + 1e-9 < floor_mm:
            raise MapPlotterError(
                f"Theme {theme_id!r} typography role {role!r} resolves to a "
                f"{cap_mm:.3g} mm cap height ({cap_role} x {cap_scale:g}), below "
                f"the {floor_mm:g} mm floor for its {physical['ink']} "
                f"{nib_mm:g} mm pen. A stroke font closes into a blot under "
                "8 x nib: raise cap_scale to at least "
                f"{floor_mm / float(type_scale[cap_role]):.3g}, or move the role "
                "to a finer nib."
            )
        gutter_mm = gutter * gap_mm
        zone_rect = zones[zone]
        if gutter_mm >= zone_rect["width"]:
            raise MapPlotterError(
                f"Theme {theme_id!r} typography role {role!r} has a "
                f"{gutter_mm:.3g} mm gutter in a {zone_rect['width']:.3g} mm wide "
                f"zone {zone!r}; nothing would be left to set."
            )
        if physical["draws"]:
            previous = claimed.get(zone)
            if previous is not None:
                raise MapPlotterError(
                    f"Theme {theme_id!r} zone overlap: roles {previous!r} and "
                    f"{role!r} both occupy zone {zone!r}. Give one of them a "
                    "different zone."
                )
            claimed[zone] = role
        resolved[role] = {
            "font_role": font_role,
            "zone": zone,
            "align": align,
            "vertical": vertical,
            "placement": f"{align}-{vertical}",
            "case": case,
            "max_lines": max_lines,
            "cap_role": cap_role,
            "cap_scale": cap_scale,
            "gutter": gutter,
        }

    occupied = sorted(claimed.items(), key=lambda pair: pair[1])
    for index, (zone_a, role_a) in enumerate(occupied):
        for zone_b, role_b in occupied[index + 1 :]:
            if _rects_overlap(zones[zone_a], zones[zone_b]):
                raise MapPlotterError(
                    f"Theme {theme_id!r} zone overlap: role {role_a!r} in zone "
                    f"{zone_a!r} and role {role_b!r} in zone {zone_b!r} cover the "
                    "same paper. Move one of them."
                )
    return {
        "policy_id": typography["policy_id"],
        "font_roles": dict(font_roles),
        "normalisation_policy_id": typography["normalisation_policy_id"],
        "roles": resolved,
    }


def _validate_copy(raw: Any, *, theme_id: str) -> dict[str, Any]:
    copy = _object(raw, field=f"{theme_id}.copy")
    _closed(copy, {"policy_id", "rules"}, field=f"{theme_id}.copy")
    if copy["policy_id"] != THEME_POLICY_IDS["copy"]:
        raise MapPlotterError(
            f"Theme {theme_id!r} references an unsupported copy policy."
        )
    rules = _object(copy["rules"], field=f"{theme_id}.copy.rules")
    if set(rules) != {"campus", "student_city", "city_preview"}:
        raise MapPlotterError(
            f"Theme {theme_id!r} must define campus, student_city, and city_preview copy."
        )
    allowed_title_tokens = {"subject.name", "city"}
    allowed_subtitle_tokens = {"country", "city-country"}
    allowed_detail_tokens = {"purpose", "coordinates", "course-disclosure"}
    for purpose, value in rules.items():
        rule = _object(value, field=f"{theme_id}.copy.rules.{purpose}")
        _closed(
            rule,
            {"title", "subtitle", "details"},
            field=f"{theme_id}.copy.rules.{purpose}",
        )
        if rule["title"] not in allowed_title_tokens:
            raise MapPlotterError(f"Theme copy title token for {purpose!r} is invalid.")
        if rule["subtitle"] not in allowed_subtitle_tokens:
            raise MapPlotterError(
                f"Theme copy subtitle token for {purpose!r} is invalid."
            )
        detail_tokens = _string_list(
            rule["details"], field=f"{theme_id}.copy.rules.{purpose}.details"
        )
        if not set(detail_tokens) <= allowed_detail_tokens or len(detail_tokens) > 3:
            raise MapPlotterError(
                f"Theme copy detail tokens for {purpose!r} are invalid."
            )
    return dict(copy)


def _validate_theme(
    raw: dict[str, Any],
    *,
    catalog_sha256: str,
    inventory: PenInventory | None = None,
    source: str = "packaged",
) -> SeriesTheme:
    expected = {
        "id",
        "version",
        "label",
        "description",
        "format",
        "export",
        "map_layers",
        "furniture",
        "typography",
        "copy",
        "placement_policy_id",
        "source_policy_id",
        "validation_policy_id",
        "batch",
    }
    _closed(raw, expected, field=raw.get("id", "themes[]"))
    theme_id = _text(raw["id"], field="themes[].id")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise MapPlotterError(f"Theme {theme_id!r} version must be a positive integer.")
    resolved_inventory = inventory or ACTUAL_PEN_INVENTORY

    format_policy = _object(raw["format"], field=f"{theme_id}.format")
    _closed(format_policy, {"contract_id", "format_id"}, field=f"{theme_id}.format")
    if (
        _text(format_policy["contract_id"], field=f"{theme_id}.format.contract_id")
        != "plate-format-v1"
    ):
        raise MapPlotterError(f"Theme {theme_id!r} must reference plate-format-v1.")
    plate = load_plate_format(
        _text(format_policy["format_id"], field=f"{theme_id}.format.format_id")
    )

    export = _validate_export(raw["export"], theme_id=theme_id)
    map_layers = _validate_map_layers(
        raw["map_layers"],
        theme_id=theme_id,
        plate=plate,
        inventory=resolved_inventory,
        canonical_args=[str(value) for value in export["canonical_args"]],
    )
    furniture = _validate_furniture(
        raw["furniture"],
        theme_id=theme_id,
        plate=plate,
        inventory=resolved_inventory,
    )
    typography = _validate_typography(
        raw["typography"],
        theme_id=theme_id,
        plate=plate,
        furniture=furniture,
        inventory=resolved_inventory,
    )
    copy = _validate_copy(raw["copy"], theme_id=theme_id)

    batch = _object(raw["batch"], field=f"{theme_id}.batch")
    _closed(
        batch,
        {"title_mode", "recommended_png_dpi", "cohort_key"},
        field=f"{theme_id}.batch",
    )
    if (
        batch["title_mode"] != "theme"
        or batch["cohort_key"] != "edition_signature_sha256"
    ):
        raise MapPlotterError(f"Theme {theme_id!r} has an unsupported batch policy.")
    recommended_png_dpi = batch["recommended_png_dpi"]
    if (
        isinstance(recommended_png_dpi, bool)
        or not isinstance(recommended_png_dpi, (int, float))
        or float(recommended_png_dpi) != 254.0
    ):
        raise MapPlotterError(
            f"Theme {theme_id!r} must use the binding 254 DPI preview raster."
        )
    for field in (
        "placement_policy_id",
        "source_policy_id",
        "validation_policy_id",
    ):
        if raw[field] != THEME_POLICY_IDS[field]:
            raise MapPlotterError(
                f"Theme {theme_id!r} references an unsupported {field}."
            )

    return SeriesTheme(
        id=theme_id,
        version=version,
        label=_text(raw["label"], field=f"{theme_id}.label"),
        description=_text(raw["description"], field=f"{theme_id}.description"),
        format={
            **format_policy,
            "zones": sorted(
                {str(record["zone"]) for record in typography["roles"].values()}
            ),
            "type_roles": list(TYPE_ROLES),
        },
        export=export,
        map_layers=map_layers,
        furniture=furniture,
        typography=typography,
        copy=copy,
        placement_policy_id=_text(
            raw["placement_policy_id"], field=f"{theme_id}.placement_policy_id"
        ),
        source_policy_id=_text(
            raw["source_policy_id"], field=f"{theme_id}.source_policy_id"
        ),
        validation_policy_id=_text(
            raw["validation_policy_id"], field=f"{theme_id}.validation_policy_id"
        ),
        batch=dict(batch),
        sha256=_stable_digest(raw),
        catalog_sha256=catalog_sha256,
        source=source,
    )


def _validate_catalog_document(
    root: Any, *, payload_sha256: str, origin: str, source: str
) -> dict[str, SeriesTheme]:
    if not isinstance(root, dict):
        raise MapPlotterError(f"Theme contract {origin} must be an object.")
    _closed(root, {"schema_version", "id", "themes"}, field=origin)
    if root["schema_version"] != THEME_SCHEMA_VERSION or root["id"] != THEME_CATALOG_ID:
        raise MapPlotterError(
            f"Theme contract {origin} has an unsupported identity; this build "
            f"reads schema {THEME_SCHEMA_VERSION} of {THEME_CATALOG_ID!r}."
        )
    raw_themes = root["themes"]
    if not isinstance(raw_themes, list) or not raw_themes:
        raise MapPlotterError(f"Theme contract {origin} has no themes.")
    themes = [
        _validate_theme(
            _object(value, field="themes[]"),
            catalog_sha256=payload_sha256,
            source=source,
        )
        for value in raw_themes
    ]
    result = {theme.id: theme for theme in themes}
    if len(result) != len(themes):
        raise MapPlotterError(f"Theme contract {origin} repeats a theme id.")
    return result


@lru_cache(maxsize=1)
def load_theme_catalog() -> dict[str, SeriesTheme]:
    payload = _resource_bytes("themes-v1.json")
    try:
        root = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Could not parse packaged theme contract {THEME_RESOURCE}: {exc}"
        ) from exc
    return _validate_catalog_document(
        root,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        origin=THEME_RESOURCE,
        source="packaged",
    )


def load_theme(theme_id: str) -> SeriesTheme:
    try:
        return load_theme_catalog()[theme_id]
    except KeyError as exc:
        choices = ", ".join(sorted(load_theme_catalog()))
        raise MapPlotterError(
            f"Unknown series theme {theme_id!r}. Choose from: {choices}."
        ) from exc


def load_theme_file(path: Path, *, theme_id: str | None = None) -> SeriesTheme:
    """Load a theme the author wrote by hand, outside the packaged catalog.

    Accepts either a whole catalog document or a single bare theme object, so a
    one-theme working file does not need the catalog wrapper.
    """

    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise MapPlotterError(f"Could not read theme file {path}: {exc}") from exc
    try:
        root = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(f"Theme file {path} is not valid JSON: {exc}") from exc
    digest = hashlib.sha256(payload).hexdigest()
    origin = str(path)
    if isinstance(root, dict) and "themes" not in root:
        theme = _validate_theme(
            _object(root, field=origin),
            catalog_sha256=digest,
            source=origin,
        )
        if theme_id is not None and theme.id != theme_id:
            raise MapPlotterError(
                f"Theme file {path} defines {theme.id!r}, not {theme_id!r}."
            )
        return theme
    catalog = _validate_catalog_document(
        root, payload_sha256=digest, origin=origin, source=origin
    )
    if theme_id is not None:
        try:
            return catalog[theme_id]
        except KeyError as exc:
            raise MapPlotterError(
                f"Theme file {path} has no theme {theme_id!r}. It defines: "
                f"{', '.join(sorted(catalog))}."
            ) from exc
    if len(catalog) != 1:
        raise MapPlotterError(
            f"Theme file {path} defines {len(catalog)} themes; name one with "
            f"{THEME_OPTION}."
        )
    return next(iter(catalog.values()))


def _option_name(token: str) -> str:
    return token.split("=", maxsplit=1)[0]


def theme_id_from_export_args(values: Sequence[object]) -> str | None:
    result: str | None = None
    for index, raw in enumerate(values):
        token = str(raw)
        if token == THEME_OPTION:
            if index + 1 >= len(values):
                raise MapPlotterError("--theme requires a theme id.")
            if result is not None:
                raise MapPlotterError("--theme may be provided only once.")
            result = str(values[index + 1])
        elif token.startswith(THEME_OPTION + "="):
            if result is not None:
                raise MapPlotterError("--theme may be provided only once.")
            result = token.split("=", maxsplit=1)[1]
    return result


def theme_file_from_export_args(values: Sequence[object]) -> Path | None:
    result: Path | None = None
    for index, raw in enumerate(values):
        token = str(raw)
        candidate: str | None = None
        if token == THEME_FILE_OPTION:
            if index + 1 >= len(values):
                raise MapPlotterError("--theme-file requires a path.")
            candidate = str(values[index + 1])
        elif token.startswith(THEME_FILE_OPTION + "="):
            candidate = token.split("=", maxsplit=1)[1]
        if candidate is None:
            continue
        if result is not None:
            raise MapPlotterError("--theme-file may be provided only once.")
        result = Path(candidate)
    return result


def theme_from_export_args(values: Sequence[object]) -> SeriesTheme | None:
    theme_path = theme_file_from_export_args(values)
    theme_id = theme_id_from_export_args(values)
    if theme_path is not None:
        return load_theme_file(theme_path, theme_id=theme_id)
    return load_theme(theme_id) if theme_id is not None else None


def expand_theme_export_args(values: Sequence[str]) -> tuple[str, ...]:
    """Inject one theme's canonical flags and reject design drift.

    Operational flags (subject, output, source snapshot, measured inventory,
    cache, timeout, and User-Agent) remain caller-controlled.
    """

    result = list(values)
    theme = theme_from_export_args(result)
    if theme is None:
        return tuple(result)
    canonical = list(theme.canonical_export_args)
    already_expanded = result[: len(canonical)] == canonical
    conflict_candidates = result[len(canonical) :] if already_expanded else result
    conflicts = sorted(
        {
            _option_name(token)
            for token in conflict_candidates
            if token.startswith("-")
            and _option_name(token) not in {THEME_OPTION, THEME_FILE_OPTION}
            and _option_name(token) in theme.locked_options
        }
    )
    if conflicts:
        raise MapPlotterError(
            f"Theme {theme.id!r} owns these design option(s): {', '.join(conflicts)}. "
            "Remove the overrides or create a new versioned theme."
        )
    return (
        tuple(result) if already_expanded else (*theme.canonical_export_args, *result)
    )


def resolve_theme_styles(
    theme: SeriesTheme,
    selected_layer_ids: set[str],
) -> list[LayerStyle]:
    """Resolve semantic map nib roles through the selected plate format.

    Layers the theme switches off are dropped here, which is the single place
    "this theme does not draw water" becomes "no water pen, no water strokes,
    no water in the manifest".
    """

    plate = load_plate_format(theme.format_id)
    role_widths = plate["map_linework_nib_mm"]
    defaults = {style.id: style for style in DEFAULT_STYLES}
    records = {str(record["id"]): record for record in theme.map_layers}
    unknown = sorted(selected_layer_ids - set(records))
    if unknown:
        raise MapPlotterError(
            f"Theme {theme.id!r} does not define selected layer(s): {', '.join(unknown)}."
        )
    resolved: list[LayerStyle] = []
    for layer_id in selected_layer_ids:
        record = records[layer_id]
        if not record["draws"]:
            continue
        nib_mm = float(role_widths[str(record["nib_role"])])
        ink = str(record["ink"])
        resolved.append(
            replace(
                defaults[layer_id],
                pen=f"{ink} {nib_mm:g}",
                ink=ink,
                nib_mm=nib_mm,
                stroke_width_mm=nib_mm,
                stroke=str(record["preview_color"]),
                order=int(record["order"]),
                strokes=1,
                passes=1,
            )
        )
    if not resolved:
        raise MapPlotterError(
            f"Theme {theme.id!r} switches off every selected map layer; there "
            "would be nothing to plot."
        )
    return sorted(resolved, key=lambda style: (style.order, style.id))


def _format_contract() -> tuple[dict[str, Any], str]:
    payload = _resource_bytes("format-v1.json")
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MapPlotterError(
            f"Could not parse packaged format contract: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise MapPlotterError("Packaged format contract must be an object.")
    return document, hashlib.sha256(payload).hexdigest()


def resolved_theme_contract(
    theme: SeriesTheme,
    *,
    styles: Sequence[LayerStyle],
    inventory: PenInventory,
    stock_tone: str,
) -> dict[str, Any]:
    """Resolve a theme into the exact invariant shared by a city cohort."""

    # Imported lazily because the renderer contract fingerprints this module as
    # one of its visual inputs.  It reads function implementations; it does not
    # call them, so this is cycle-free while keeping the edition bound to the
    # code that can change plotted geometry.
    from .render_contract import visual_renderer_contract

    format_document, format_sha = _format_contract()
    if format_document.get("id") != theme.format["contract_id"]:
        raise MapPlotterError(
            f"Theme {theme.id!r} format contract id does not match the installed resource."
        )
    plate = load_plate_format(theme.format_id)
    selected_plate_sha = _stable_digest(plate)
    allowed_nibs = tuple(float(value) for value in plate["nib_ladder_mm"])
    theme_layers = {str(record["id"]): record for record in theme.map_layers}
    layer_plan: list[dict[str, Any]] = []
    for style in styles:
        record = theme_layers.get(style.id)
        if record is None:
            raise MapPlotterError(
                f"Resolved style {style.id!r} is outside theme {theme.id!r}."
            )
        if not record["draws"]:
            raise MapPlotterError(
                f"Theme {theme.id!r} switches map layer {style.id!r} off, but a "
                "style for it reached the renderer."
            )
        assert style.ink is not None and style.nib_mm is not None
        expected_width = float(plate["map_linework_nib_mm"][str(record["nib_role"])])
        expected = {
            "ink": str(record["ink"]),
            "stroke": str(record["preview_color"]),
            "order": int(record["order"]),
            "nib_mm": expected_width,
        }
        actual = {
            "ink": style.ink,
            "stroke": style.stroke,
            "order": style.order,
            "nib_mm": style.nib_mm,
        }
        if actual != expected or style.strokes != 1 or style.passes != 1:
            raise MapPlotterError(
                f"Resolved style {style.id!r} does not match theme {theme.id!r}; "
                "palette, role width, order, strokes, and passes are immutable."
            )
        fit = fit_pen_width(
            inventory,
            ink=style.ink,
            requested_width_mm=expected_width,
            allowed_nibs_mm=allowed_nibs,
        )
        if fit.stroke_count != 1 or abs(fit.pen.nominal_nib_mm - expected_width) > 1e-9:
            raise MapPlotterError(
                f"Theme map layer {style.id!r} requires a one-pass nominal "
                f"{style.ink} {expected_width:g} mm pen; inventory "
                f"{inventory.id!r} resolves it to nominal "
                f"{fit.pen.nominal_nib_mm:g} mm with {fit.stroke_count} stroke(s)."
            )
        layer_plan.append(
            {
                "layer_id": style.id,
                "label": style.label,
                "order": style.order,
                "ink": style.ink,
                "nib_role": str(record["nib_role"]),
                "target_width_mm": _manifest_mm(expected_width),
                "preview_color": style.stroke,
                "pen_id": fit.pen.identity,
                "nominal_nib_mm": _manifest_mm(fit.pen.nominal_nib_mm),
                "effective_width_mm": _manifest_mm(fit.pen.mark_width_mm),
                "stroke_count": fit.stroke_count,
                "offset_pitch_mm": _manifest_mm(fit.offset_pitch_mm),
                "plotted_width_mm": _manifest_mm(fit.plotted_width_mm),
                "width_error_mm": _manifest_mm(fit.width_error_mm),
                "fit_mode": fit.mode,
                "passes": 1,
            }
        )
    layer_plan.sort(key=lambda item: (int(item["order"]), str(item["layer_id"])))

    physical_layer_plan: list[dict[str, Any]] = []
    physical_effective_widths: dict[str, float] = {}
    for layer_id in FURNITURE_LAYERS:
        record = theme.furniture[layer_id]
        target_width = float(plate["nib_roles_mm"][str(record["nib_role"])])
        fit = fit_pen_width(
            inventory,
            ink=str(record["ink"]),
            requested_width_mm=target_width,
            allowed_nibs_mm=allowed_nibs,
        )
        if fit.stroke_count != 1 or abs(fit.pen.nominal_nib_mm - target_width) > 1e-9:
            raise MapPlotterError(
                f"Theme furniture layer {layer_id!r} requires a safe one-pass "
                f"nominal {record['ink']} {target_width:g} mm pen; inventory "
                f"{inventory.id!r} resolves it to nominal "
                f"{fit.pen.nominal_nib_mm:g} mm with {fit.stroke_count} stroke(s)."
            )
        physical_effective_widths[layer_id] = fit.pen.mark_width_mm
        weight = int(record["weight"])
        # Type weight is the road weight engine: n parallel offsets at 0.85 nib.
        pitch_mm = 0.0 if weight == 1 else 0.85 * fit.pen.mark_width_mm
        plotted_width_mm = weighted_mark_width_mm(
            nib_mm=fit.pen.mark_width_mm, stroke_count=weight
        )
        physical_layer_plan.append(
            {
                "layer_id": layer_id,
                "ink": str(record["ink"]),
                "nib_role": str(record["nib_role"]),
                "target_width_mm": _manifest_mm(target_width),
                "preview_color": str(record["preview_color"]),
                "emission": "required" if record["draws"] else "forbidden",
                "pen_id": fit.pen.identity,
                "nominal_nib_mm": _manifest_mm(fit.pen.nominal_nib_mm),
                "effective_width_mm": _manifest_mm(fit.pen.mark_width_mm),
                "stroke_count": weight,
                "offset_pitch_mm": _manifest_mm(pitch_mm),
                "plotted_width_mm": _manifest_mm(plotted_width_mm),
                "width_error_mm": _manifest_mm(plotted_width_mm - target_width),
                "fit_mode": fit.mode,
                "passes": 1,
            }
        )

    inventory_document = inventory.as_dict()
    inventory_sha = _stable_digest(inventory_document)
    type_scale = plate["type_scale_mm"]
    gap_mm = float(plate["gap_mm"])
    typography_roles: dict[str, Any] = {}
    for role, record in theme.typography["roles"].items():
        physical_layer_id = ROLE_PHYSICAL_LAYERS[role]
        effective_nib = _manifest_mm(physical_effective_widths[physical_layer_id])
        minimum_cap = _manifest_mm(
            max(float(plate["rules"]["min_cap_height_mm"][role]), 8.0 * effective_nib)
        )
        cap_mm = float(type_scale[str(record["cap_role"])]) * float(
            record["cap_scale"]
        )
        preferred_cap = _manifest_mm(max(cap_mm, minimum_cap))
        typography_roles[role] = {
            **dict(record),
            "preferred_cap_height_mm": preferred_cap,
            "minimum_cap_height_mm": minimum_cap,
            "inset_mm": _manifest_mm(float(record["gutter"]) * gap_mm),
            "weight_strokes": int(theme.furniture[physical_layer_id]["weight"]),
            "nib_role": str(theme.furniture[physical_layer_id]["nib_role"]),
            "target_nib_mm": float(
                plate["nib_roles_mm"][
                    str(theme.furniture[physical_layer_id]["nib_role"])
                ]
            ),
            "physical_layer_id": physical_layer_id,
        }
    visual_renderer = visual_renderer_contract()
    decoration = {
        "border_style": str(theme.furniture["poster_border"]["style"]),
        "north_mark": str(theme.furniture["map_furniture"]["north_mark"]),
        "scale_bar": bool(theme.furniture["map_furniture"]["scale_bar"]),
        "divider_rule": False,
    }
    invariant = {
        "theme_id": theme.id,
        "theme_sha256": theme.sha256,
        "format_id": theme.format_id,
        "format_contract_id": format_document.get("id"),
        "format_selected_plate_sha256": selected_plate_sha,
        "font_sha256": STROKE_FONT_SHA256,
        "inventory_id": inventory.id,
        "inventory_sha256": inventory_sha,
        "stock_tone": stock_tone,
        "resolved_map_layers": layer_plan,
        "resolved_physical_layers": physical_layer_plan,
        "typography_policy_id": theme.typography["policy_id"],
        "typography_roles": typography_roles,
        "decoration": decoration,
        "copy_policy_id": theme.copy["policy_id"],
        "placement_policy_id": theme.placement_policy_id,
        "source_policy_id": theme.source_policy_id,
        "validation_policy_id": theme.validation_policy_id,
        "visual_renderer_contract_sha256": visual_renderer["sha256"],
    }
    return {
        "schema_version": THEME_SCHEMA_VERSION,
        "theme_id": theme.id,
        "theme_version": theme.version,
        "theme_sha256": theme.sha256,
        "theme_catalog_id": THEME_CATALOG_ID,
        "theme_catalog_sha256": theme.catalog_sha256,
        "theme_source": theme.source,
        "format": {
            "id": theme.format_id,
            "contract_id": format_document.get("id"),
            "contract_sha256": format_sha,
            "selected_plate_sha256": selected_plate_sha,
            "zones": list(theme.format["zones"]),
        },
        "font": stroke_font_contract(),
        "typography": {
            "policy_id": theme.typography["policy_id"],
            "roles": typography_roles,
        },
        "decoration": decoration,
        "copy_policy_id": theme.copy["policy_id"],
        "placement_policy_id": theme.placement_policy_id,
        "source_policy_id": theme.source_policy_id,
        "validation_policy_id": theme.validation_policy_id,
        "visual_renderer_contract": visual_renderer,
        "batch": dict(theme.batch),
        "inventory": {
            "id": inventory.id,
            "sha256": inventory_sha,
            "stock_tone": stock_tone,
        },
        "resolved_map_layers": layer_plan,
        "resolved_physical_layers": physical_layer_plan,
        "edition_signature_sha256": _stable_digest(invariant),
    }


@dataclass(frozen=True)
class SubjectCopy:
    title: str
    subtitle: str
    details: tuple[str, ...]
    policy_id: str
    rule_id: str


def resolve_subject_copy(
    theme: SeriesTheme,
    subject: CatalogSubject,
    layout: Layout,
) -> SubjectCopy:
    """Resolve closed copy tokens for one catalog subject."""

    rule = theme.copy["rules"][subject.map_purpose]
    title = subject.name if rule["title"] == "subject.name" else subject.city
    subtitle = (
        subject.country
        if rule["subtitle"] == "country"
        else f"{subject.city} / {subject.country}"
    )
    latitude, longitude = layout.bbox.center
    latitude_label = f"{abs(latitude):.4f} {'N' if latitude >= 0 else 'S'}"
    longitude_label = f"{abs(longitude):.4f} {'E' if longitude >= 0 else 'W'}"
    values = {
        "purpose": {
            "campus": "UNIVERSITY CAMPUS",
            "student_city": "STUDENT CITY",
            "city_preview": "CITY BASEMAP PREVIEW",
        }[subject.map_purpose],
        "course-disclosure": "COURSE NOT INCLUDED",
        "coordinates": f"{latitude_label} / {longitude_label}",
    }
    details = tuple(values[token] for token in rule["details"])
    return SubjectCopy(
        title=title,
        subtitle=subtitle,
        details=details,
        policy_id=str(theme.copy["policy_id"]),
        rule_id=subject.map_purpose,
    )
