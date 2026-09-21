from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import UTC, datetime
from math import hypot
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .cartography import (
    DETAIL_PROFILE_CHOICES,
    FULL_CARTOGRAPHY_DETAIL_PROFILES,
    INK_BUDGETED_DETAIL_PROFILES,
    PHYSICAL_MINIMUM_GATED_DETAIL_PROFILES,
    SOURCE_COMPLETE_DETAIL_PROFILES,
    CartographyResult,
    prepare_map_strokes,
)
from .completeness import audit_highway_completeness, audit_raw_geometry_integrity
from .display_font import display_font_contract
from .furniture import (
    FurniturePolicy,
    MEMORABILIA_VARIANTS,
    append_attribution,
    append_map_frame,
    append_map_furniture,
    append_poster_decoration,
    append_course_labels,
    append_race_course,
    append_rowing_crew_copy,
    knock_out_labels,
    plan_course_labels,
    bind_design_contract_groups,
    coordinate_label,
    default_furniture_policy,
    design_contract_identity,
    furniture_policy_from_contract,
    layout_plate_format,
    typography_evidence,
    with_crew_zones,
    with_split_zones,
)
from .geometry import (
    A5_POSTER_PRESETS,
    Layout,
    load_plate_format,
    polyline_length,
)
from .models import (
    AcquisitionResult,
    LayerStyle,
    MapFeature,
    MapPlotterError,
    PlotStroke,
)
from .ink_budget import select_ink_balanced_strokes
from .physical import ROAD_STYLE_CHOICES, compile_physical_strokes
from .pens import PenInventory, fit_pen_width, style_pen_width
from .plotopt import OptimisationReport, TimingConfig, measure_plot, optimise_strokes
from .quality import physical_resolution_report
from .svgkit import (
    INKSCAPE_NS,
    MAP_NS,
    SODIPODI_NS,
    decoration_pen_plan as _decoration_pen_plan,
    format_measurement as _format_measurement,
    format_number as _format_number,
    layer_stats as _layer_stats,
    path_data as _path_data,
    physical_group_attributes as _physical_group_attributes,
    plot_path_attributes as _plot_path_attributes,
    svg_tag as _svg,
)


POSTER_LAYOUT_CHOICES = frozenset(
    {
        "city-map",
        "classic",
        "university-memorabilia",
        "rowing-course",
        "rowing-crew",
    }
)
#: Layouts that carry their own header compass and suppress the map-overlay
#: furniture the classic stack puts in the field.
HEADER_COMPASS_LAYOUTS = frozenset(
    {"city-map", "university-memorabilia", "rowing-course", "rowing-crew"}
)
#: Layouts that give map height back to their own bands.
CREW_LAYOUTS = frozenset({"rowing-crew"})


def _generation_timestamp() -> str:
    """Return a reproducible UTC timestamp when SOURCE_DATE_EPOCH is set."""

    epoch_text = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch_text is None:
        return datetime.now(UTC).isoformat()
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", epoch_text) is None:
        raise MapPlotterError(
            "SOURCE_DATE_EPOCH must be a canonical non-negative integer."
        )
    try:
        return datetime.fromtimestamp(int(epoch_text), UTC).isoformat()
    except (OSError, OverflowError, ValueError) as exc:
        raise MapPlotterError(
            "SOURCE_DATE_EPOCH is outside the supported timestamp range."
        ) from exc


# The furniture module owns every non-map mark on the sheet.  These aliases keep
# the historical private names importable while the drawing itself lives there.
_design_contract_identity = design_contract_identity
_bind_design_contract_groups = bind_design_contract_groups
_typography_evidence = typography_evidence
_layout_plate_format = layout_plate_format


def _validate_resolved_theme_layers(
    contract: dict[str, Any],
    layer_stats: list[dict[str, Any]],
    root: ET.Element,
) -> None:
    expected_map = contract.get("resolved_map_layers")
    expected_physical = contract.get("resolved_physical_layers")
    if not isinstance(expected_map, list) or not isinstance(expected_physical, list):
        raise MapPlotterError(
            "A design contract must include resolved map and physical layers."
        )
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
        if local_name in {"style", "link"}:
            raise MapPlotterError(
                "Themed SVGs cannot contain stylesheet elements; physical "
                "appearance must be explicit on contracted layers."
            )
        if local_name in unsupported_graphics | active_content:
            raise MapPlotterError(
                f"Themed SVG contains unsupported drawable or active element "
                f"<{local_name}>; contracted artwork is path-only."
            )
        forbidden = css_override_attributes & set(element.attrib)
        if element.tag == _svg("path"):
            forbidden |= path_presentation_attributes & set(element.attrib)
        if forbidden:
            raise MapPlotterError(
                "Themed SVG contains CSS/presentation override(s) that can "
                "bypass its physical layer contract: "
                f"{', '.join(sorted(forbidden))}."
            )

    def assert_physical_match(
        layer_id: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> None:
        checks = {
            "ink": expected.get("ink"),
            "preview_color": expected.get("preview_color"),
            "pen_id": expected.get("pen_id"),
            "nominal_nib_mm": expected.get("nominal_nib_mm"),
            "nib_mm": expected.get("effective_width_mm"),
            "requested_width_mm": expected.get("target_width_mm"),
            "strokes": expected.get("stroke_count"),
            "passes": expected.get("passes"),
            "offset_pitch_mm": expected.get("offset_pitch_mm"),
            "plotted_width_mm": expected.get("plotted_width_mm"),
            "width_fit_error_mm": expected.get("width_error_mm"),
            "width_fit_mode": expected.get("fit_mode"),
        }
        if any(actual.get(key) != value for key, value in checks.items()):
            raise MapPlotterError(
                f"Rendered physical plan for {layer_id!r} drifted from the "
                "resolved theme."
            )

    map_ids = {
        str(record.get("layer_id"))
        for record in expected_map
        if isinstance(record, dict)
    }
    actual_map: dict[str, list[dict[str, Any]]] = {layer_id: [] for layer_id in map_ids}
    for layer in layer_stats:
        logical_id = layer.get("logical_layer_id")
        if logical_id in actual_map:
            actual_map[str(logical_id)].append(layer)
    for expected in expected_map:
        if not isinstance(expected, dict):
            raise MapPlotterError("A resolved theme map layer must be an object.")
        layer_id = str(expected.get("layer_id"))
        matches = actual_map.get(layer_id, [])
        if len(matches) != 1:
            raise MapPlotterError(
                f"Rendered manifest must contain exactly one theme map-layer "
                f"record for {layer_id!r}; found {len(matches)}."
            )
        assert_physical_match(layer_id, expected, matches[0])

    physical_ids = {
        str(record.get("layer_id"))
        for record in expected_physical
        if isinstance(record, dict)
    }
    actual_physical: dict[str, list[dict[str, Any]]] = {
        layer_id: [] for layer_id in physical_ids
    }
    for layer in layer_stats:
        actual_layer_id = layer.get("id")
        if actual_layer_id in actual_physical and layer.get("logical_layer_id") is None:
            actual_physical[str(actual_layer_id)].append(layer)
    for expected in expected_physical:
        if not isinstance(expected, dict):
            raise MapPlotterError("A resolved theme physical layer must be an object.")
        layer_id = str(expected.get("layer_id"))
        emission = expected.get("emission")
        matches = actual_physical.get(layer_id, [])
        if emission == "forbidden":
            if matches:
                raise MapPlotterError(
                    f"Theme forbids physical layer {layer_id!r}, but it was emitted."
                )
            continue
        if emission != "required" or len(matches) != 1 or not matches[0].get("emitted"):
            raise MapPlotterError(
                f"Theme requires exactly one emitted physical layer {layer_id!r}."
            )
        assert_physical_match(layer_id, expected, matches[0])

    unexpected_layers = sorted(
        {
            str(layer.get("logical_layer_id") or layer.get("id"))
            for layer in layer_stats
            if str(layer.get("logical_layer_id") or layer.get("id"))
            not in map_ids | physical_ids
        }
    )
    if unexpected_layers:
        raise MapPlotterError(
            "Themed artwork contains uncontracted layer record(s): "
            f"{', '.join(unexpected_layers)}."
        )
    declared_groups = {
        str(layer["svg_group_id"])
        for layer in layer_stats
        if layer.get("emitted") and layer.get("svg_group_id") is not None
    }
    emitted_layers = [
        layer
        for layer in layer_stats
        if layer.get("emitted") and layer.get("svg_group_id") is not None
    ]
    if len(declared_groups) != len(emitted_layers):
        raise MapPlotterError(
            "Themed physical manifest repeats an emitted SVG group identity."
        )
    direct_groups: dict[str, list[ET.Element]] = {}
    for group in root.findall(_svg("g")):
        group_id = group.get("id")
        if group_id is not None:
            direct_groups.setdefault(group_id, []).append(group)
    actual_groups = {
        str(group.get("id"))
        for group in root.findall(_svg("g"))
        if group.get(f"{{{INKSCAPE_NS}}}groupmode") == "layer"
        and group.get("id") is not None
    }
    if actual_groups != declared_groups:
        missing = sorted(declared_groups - actual_groups)
        unexpected = sorted(actual_groups - declared_groups)
        raise MapPlotterError(
            "Themed SVG layer groups do not exactly match the physical manifest "
            f"(missing={missing}, unexpected={unexpected})."
        )
    repeated_groups = sorted(
        group_id
        for group_id, matches in direct_groups.items()
        if group_id in declared_groups and len(matches) != 1
    )
    if repeated_groups:
        raise MapPlotterError(
            "Themed SVG repeats declared top-level layer group(s): "
            f"{', '.join(repeated_groups)}."
        )

    escaped_path_owners: list[str] = []
    for child in root:
        contains_path = child.tag == _svg("path") or any(
            descendant.tag == _svg("path") for descendant in child.iter()
        )
        if not contains_path:
            continue
        child_id = child.get("id", child.tag.rsplit("}", maxsplit=1)[-1])
        if (
            child.tag != _svg("g")
            or child_id not in declared_groups
            or child.get(f"{{{INKSCAPE_NS}}}groupmode") != "layer"
        ):
            escaped_path_owners.append(child_id)
    if escaped_path_owners:
        raise MapPlotterError(
            "Themed SVG contains plottable geometry outside its contracted "
            f"top-level layers: {', '.join(sorted(escaped_path_owners))}."
        )

    numeric_attributes = {
        "data-plot-nib-mm": "nib_mm",
        "data-plot-nominal-nib-mm": "nominal_nib_mm",
        "data-plot-width-mm": "plotted_width_mm",
        "data-plot-requested-width-mm": "requested_width_mm",
        "data-plot-width-fit-error-mm": "width_fit_error_mm",
        "data-plot-offset-pitch-mm": "offset_pitch_mm",
    }
    integer_attributes = {
        "data-plot-strokes": "strokes",
        "data-plot-passes": "passes",
    }
    text_attributes = {
        "data-plot-ink": "ink",
        "data-plot-width-fit-mode": "width_fit_mode",
        "data-plot-pen-profile": "pen_profile",
        "data-plot-pen-id": "pen_id",
        "data-plot-calibration-state": "calibration_state",
        "data-plot-calibration-substrate": "calibration_substrate",
    }
    square_layers = {"frame", "poster_border"}
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
    for layer in emitted_layers:
        group_id = str(layer["svg_group_id"])
        group = direct_groups[group_id][0]
        unexpected_group_attributes = sorted(
            attribute
            for attribute in group.attrib
            if attribute not in allowed_group_attributes
            and not attribute.startswith("data-")
        )
        if unexpected_group_attributes:
            raise MapPlotterError(
                f"Themed SVG layer {group_id!r} has unsupported attribute(s): "
                f"{', '.join(unexpected_group_attributes)}."
            )
        for child in group:
            child_name = child.tag.rsplit("}", maxsplit=1)[-1]
            if child.tag not in {_svg("path"), _svg("title")}:
                raise MapPlotterError(
                    f"Themed SVG layer {group_id!r} contains nested <{child_name}>; "
                    "layer descendants must be direct paths or titles."
                )
            if child.tag == _svg("title"):
                if child.attrib or list(child):
                    raise MapPlotterError(
                        f"Themed SVG layer {group_id!r} has a non-plain title node."
                    )
                continue
            unexpected_path_attributes = sorted(
                attribute
                for attribute in child.attrib
                if attribute != "d" and not attribute.startswith("data-")
            )
            if unexpected_path_attributes:
                raise MapPlotterError(
                    f"Themed SVG layer {group_id!r} path has unsupported "
                    f"attribute(s): {', '.join(unexpected_path_attributes)}."
                )
            for path_child in child:
                if (
                    path_child.tag != _svg("title")
                    or path_child.attrib
                    or list(path_child)
                ):
                    path_child_name = path_child.tag.rsplit("}", maxsplit=1)[-1]
                    raise MapPlotterError(
                        f"Themed SVG layer {group_id!r} path contains unsupported "
                        f"child <{path_child_name}>."
                    )
        if group.get("fill") != "none":
            raise MapPlotterError(
                f"Themed SVG layer {group_id!r} must explicitly use fill='none'."
            )
        if group.get("stroke") != layer.get("preview_color"):
            raise MapPlotterError(
                f"Themed SVG layer {group_id!r} preview ink drifted from its manifest."
            )
        try:
            group_stroke_width = float(group.get("stroke-width", ""))
        except ValueError as exc:
            raise MapPlotterError(
                f"Themed SVG layer {group_id!r} has no numeric stroke width."
            ) from exc
        if abs(group_stroke_width - float(layer["nib_mm"])) > 1e-9:
            raise MapPlotterError(
                f"Themed SVG layer {group_id!r} stroke width drifted from its nib."
            )
        for attribute, field in numeric_attributes.items():
            try:
                actual = float(group.get(attribute, ""))
                expected = float(layer[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise MapPlotterError(
                    f"Themed SVG layer {group_id!r} has malformed {attribute}."
                ) from exc
            if abs(actual - expected) > 1e-9:
                raise MapPlotterError(
                    f"Themed SVG layer {group_id!r} {attribute} drifted from "
                    "its physical manifest."
                )
        for attribute, field in integer_attributes.items():
            try:
                actual_integer = int(group.get(attribute, ""))
                expected_integer = int(layer[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise MapPlotterError(
                    f"Themed SVG layer {group_id!r} has malformed {attribute}."
                ) from exc
            if actual_integer != expected_integer:
                raise MapPlotterError(
                    f"Themed SVG layer {group_id!r} {attribute} drifted from "
                    "its physical manifest."
                )
        for attribute, field in text_attributes.items():
            actual_text = group.get(attribute)
            expected_value = layer.get(field)
            expected_text = None if expected_value is None else str(expected_value)
            if actual_text != expected_text:
                raise MapPlotterError(
                    f"Themed SVG layer {group_id!r} {attribute} drifted from "
                    "its physical manifest."
                )
        descendant_paths = list(group.iter(_svg("path")))
        if len(descendant_paths) != int(layer["path_count"]):
            raise MapPlotterError(
                f"Themed SVG layer {group_id!r} path count drifted from its manifest."
            )
        layer_id = str(layer.get("logical_layer_id") or layer.get("id"))
        expected_linecap = "butt" if layer_id in square_layers else "round"
        expected_linejoin = "miter" if layer_id in square_layers else "round"
        if (
            group.get("stroke-linecap") != expected_linecap
            or group.get("stroke-linejoin") != expected_linejoin
        ):
            raise MapPlotterError(
                f"Themed SVG layer {group_id!r} violates the series line-cap/join rule."
            )


def _stroke_source_ref_set(strokes: list[PlotStroke]) -> set[str]:
    result: set[str] = set()
    for stroke in strokes:
        result.update(
            item for item in stroke.tags.get("source-refs", "").split(";") if item
        )
    return result


def _source_object_ref(source_ref: str) -> str:
    object_type, separator, remainder = source_ref.partition("/")
    object_id, _separator, _part = remainder.partition("/")
    return (
        f"{object_type}/{object_id}"
        if separator and object_type in {"way", "relation"}
        else source_ref
    )


def _required_landmark_source_tags(
    data: dict[str, Any], landmark_refs: tuple[str, ...]
) -> dict[str, dict[str, str] | None]:
    """Index exact requested objects before semantic feature extraction."""

    indexed: dict[str, dict[str, str] | None] = {
        required_ref: None for required_ref in landmark_refs
    }
    elements = data.get("elements")
    if not isinstance(elements, list):
        return indexed
    for element in elements:
        if not isinstance(element, dict):
            continue
        object_type = element.get("type")
        object_id = element.get("id")
        if object_type not in {"way", "relation"} or object_id is None:
            continue
        object_ref = f"{object_type}/{object_id}"
        if object_ref not in indexed or indexed[object_ref] is not None:
            continue
        tags_value = element.get("tags")
        indexed[object_ref] = (
            {str(key): str(value) for key, value in tags_value.items()}
            if isinstance(tags_value, dict)
            else {}
        )
    return indexed


def _serialized_polyline_length(stroke: PlotStroke) -> float:
    """Measure the exact 0.001 mm coordinates emitted for a physical path."""

    serialized = [(float(f"{x:.3f}"), float(f"{y:.3f}")) for x, y in stroke.points]
    return polyline_length(serialized)


def _ink_budget_effective_nibs(
    styles: list[LayerStyle],
    layout: Layout,
    *,
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> dict[str, float]:
    """Resolve the physical marks used by ink-balanced planning.

    The bundled plot profile has a matching physical nib for every requested
    line width.  A custom inventory may still use a measured effective mark;
    this function deliberately records that mark rather than the nominal size.
    """

    resolved: dict[str, float] = {}
    for style in styles:
        assert style.ink is not None and style.nib_mm is not None
        requested_width_mm = (
            style.nib_mm
            if style.id
            in {
                "road_areas",
                "roads_major",
                "roads_secondary",
                "roads_local",
                "roads_other",
                "paths",
            }
            else style.plotted_width_mm
        )
        plan = (
            fit_pen_width(
                pen_inventory,
                ink=style.ink,
                requested_width_mm=requested_width_mm,
                allowed_nibs_mm=allowed_nibs_mm,
            )
            if pen_inventory is not None
            else style_pen_width(
                ink=style.ink,
                nib_mm=style.nib_mm,
                stroke_count=(
                    1
                    if style.id
                    in {
                        "road_areas",
                        "roads_major",
                        "roads_secondary",
                        "roads_local",
                        "roads_other",
                        "paths",
                    }
                    else style.strokes
                ),
            )
        )
        resolved[style.id] = plan.pen.mark_width_mm

    format_id = f"{layout.page.name.casefold()}-{layout.page.orientation}"
    plate_format = load_plate_format(format_id)
    frame_plan = _decoration_pen_plan(
        ink="Black",
        requested_width_mm=float(plate_format["nib_roles_mm"]["primary"]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    north_plan = _decoration_pen_plan(
        ink="Black",
        requested_width_mm=float(plate_format["nib_roles_mm"]["hairline"]),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
    )
    resolved["frame"] = frame_plan.pen.mark_width_mm
    resolved["north"] = north_plan.pen.mark_width_mm
    return resolved


def _single_stroke_effective_layer_nib(
    styles: list[LayerStyle],
    layer_id: str,
    *,
    purpose: str,
    pen_inventory: PenInventory | None,
    allowed_nibs_mm: tuple[float, ...] | None,
) -> float:
    """Resolve a pattern budget from the physical pen actually drawing it."""

    style = next((candidate for candidate in styles if candidate.id == layer_id), None)
    if style is None or style.ink is None or style.nib_mm is None:
        raise MapPlotterError(
            f"{purpose} requires an enabled {layer_id!r} physical style."
        )
    plan = (
        fit_pen_width(
            pen_inventory,
            ink=style.ink,
            requested_width_mm=style.plotted_width_mm,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        if pen_inventory is not None
        else style_pen_width(
            ink=style.ink,
            nib_mm=style.nib_mm,
            stroke_count=style.strokes,
        )
    )
    if plan.stroke_count != 1:
        raise MapPlotterError(
            f"{purpose} requires one physical stroke, but layer {layer_id!r} "
            f"resolves to {plan.stroke_count}."
        )
    return plan.pen.mark_width_mm


def _budgeted_source_lineage(
    pre_budget_strokes: list[PlotStroke],
    retained_strokes: list[PlotStroke],
    pre_budget_lineage: dict[str, Any],
    budget_ledger: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile the post-budget cartographic source ledger exactly."""

    visible_refs = _stroke_source_ref_set(pre_budget_strokes)
    retained_refs = _stroke_source_ref_set(retained_strokes)
    omitted_refs = sorted(visible_refs - retained_refs)
    evidenced_refs = {
        str(source_ref) for source_ref in budget_ledger.get("omitted_source_refs", [])
    }
    if set(omitted_refs) - evidenced_refs:
        preview = ", ".join(sorted(set(omitted_refs) - evidenced_refs)[:5])
        raise MapPlotterError(
            "Ink-balanced cartography omitted source geometry without budget "
            f"evidence ({preview})."
        )
    if retained_refs & evidenced_refs:
        preview = ", ".join(sorted(retained_refs & evidenced_refs)[:5])
        raise MapPlotterError(
            "Ink-budget evidence claims source geometry that remains selected "
            f"({preview})."
        )

    visible_by_layer: dict[str, set[str]] = {}
    retained_by_layer: dict[str, set[str]] = {}
    for stroke in pre_budget_strokes:
        visible_by_layer.setdefault(stroke.layer, set()).update(
            item for item in stroke.tags.get("source-refs", "").split(";") if item
        )
    for stroke in retained_strokes:
        retained_by_layer.setdefault(stroke.layer, set()).update(
            item for item in stroke.tags.get("source-refs", "").split(";") if item
        )

    return {
        "visible_source_ref_count": len(visible_refs),
        "emitted_source_ref_count": len(visible_refs & retained_refs),
        "omitted_source_ref_count": len(omitted_refs),
        "omitted_source_refs": omitted_refs,
        "omission_reason": "ink_budget_gate",
        "omission_evidence_complete": True,
        "pre_budget_source_lineage": deepcopy(pre_budget_lineage),
        "by_source_layer": {
            layer: {
                "visible": len(refs),
                "emitted": len(refs & retained_by_layer.get(layer, set())),
                "omitted": len(refs - retained_by_layer.get(layer, set())),
            }
            for layer, refs in sorted(visible_by_layer.items())
        },
    }


def _styles_by_physical_pen(styles: list[LayerStyle]) -> list[LayerStyle]:
    """Keep first-seen pen order while making every matching pen contiguous."""

    keys: list[tuple[str, float]] = []
    grouped: dict[tuple[str, float], list[LayerStyle]] = {}
    for style in styles:
        key = style.physical_pen_identity
        if key not in grouped:
            keys.append(key)
            grouped[key] = []
        grouped[key].append(style)
    return [style for key in keys for style in grouped[key]]


def _layer_stat_pen_key(layer: dict[str, Any]) -> tuple[str, str]:
    ink = str(layer.get("ink", "")).casefold()
    try:
        nominal_nib_mm = round(
            float(layer.get("nominal_nib_mm", layer.get("nib_mm", ""))), 6
        )
    except (TypeError, ValueError) as exc:
        raise MapPlotterError(
            f"Layer statistics for {layer.get('id')!r} lack a numeric nominal nib."
        ) from exc
    return (
        str(layer.get("pen_profile", "style")),
        str(layer.get("pen_id") or f"{ink}-{_format_number(nominal_nib_mm)}"),
    )


def _reorder_document_layers_by_pen(
    root: ET.Element,
    layer_stats: list[dict[str, Any]],
    *,
    fixed_inventory_slots: bool = False,
) -> None:
    """Make top-level physical pen domains contiguous and renumber labels."""

    groups = [
        element
        for element in root
        if element.tag == _svg("g") and element.get("id", "").startswith("layer-")
    ]
    keys: list[tuple[str, str]] = []
    buckets: dict[tuple[str, str], list[ET.Element]] = {}
    for group in groups:
        ink = group.get("data-plot-ink", "").casefold()
        try:
            nominal_nib_mm = round(
                float(
                    group.get(
                        "data-plot-nominal-nib-mm",
                        group.get("data-plot-nib-mm", ""),
                    )
                ),
                6,
            )
        except ValueError as exc:
            raise MapPlotterError(
                f"SVG layer {group.get('id')!r} lacks a numeric nominal nib."
            ) from exc
        pen_id = group.get(
            "data-plot-pen-id", f"{ink}-{_format_number(nominal_nib_mm)}"
        )
        profile = group.get("data-plot-pen-profile", "style")
        key = (profile, pen_id)
        if key not in buckets:
            keys.append(key)
            buckets[key] = []
        buckets[key].append(group)

    def finish_priority(key: tuple[str, str]) -> int:
        ink = buckets[key][0].get("data-plot-ink", "").casefold()
        if ink in {"gold", "silver"}:
            return 2
        if ink == "white":
            return 1
        return 0

    if fixed_inventory_slots:
        configured_keys = list(
            dict.fromkeys(_layer_stat_pen_key(layer) for layer in layer_stats)
        )
        unexpected = sorted(set(keys) - set(configured_keys))
        if unexpected:
            raise MapPlotterError(
                "Emitted SVG uses pen domains absent from the fixed inventory "
                f"slots: {unexpected}."
            )
        pen_order = configured_keys
    else:
        pen_order = [
            key
            for _, key in sorted(
                enumerate(keys),
                key=lambda item: (finish_priority(item[1]), item[0]),
            )
        ]
    ordered = [group for key in pen_order if key in buckets for group in buckets[key]]
    stats_by_id = {str(item["id"]): item for item in layer_stats}
    step_by_key = {key: step for step, key in enumerate(pen_order, start=1)}
    if fixed_inventory_slots:
        for layer in layer_stats:
            layer["pen_step"] = step_by_key[_layer_stat_pen_key(layer)]
    for layer_number, group in enumerate(ordered, start=1):
        ink = group.get("data-plot-ink", "").casefold()
        nominal_nib_mm = round(
            float(
                group.get(
                    "data-plot-nominal-nib-mm",
                    group.get("data-plot-nib-mm", ""),
                )
            ),
            6,
        )
        key = (
            group.get("data-plot-pen-profile", "style"),
            group.get("data-plot-pen-id", f"{ink}-{_format_number(nominal_nib_mm)}"),
        )
        pen_step = step_by_key[key]
        group.set("data-pen-step", str(pen_step))
        label_key = f"{{{INKSCAPE_NS}}}label"
        old_label = group.get(label_key, group.get("id", "layer"))
        description = old_label.split(" — ", maxsplit=1)[-1]
        new_label = f"{layer_number:02d} — {description}"
        group.set(label_key, new_label)
        layer_id = group.get("id", "").removeprefix("layer-")
        if layer_id in stats_by_id:
            stats_by_id[layer_id]["svg_layer_label"] = new_label
            stats_by_id[layer_id]["pen_step"] = pen_step

    for group in groups:
        root.remove(group)
    for group in ordered:
        root.append(group)


def _plot_metrics_dict(metrics: Any) -> dict[str, float | int]:
    return {
        "pen_down_distance_mm": round(metrics.pen_down_distance_mm, 1),
        "pen_up_travel_mm": round(metrics.pen_up_travel_mm, 1),
        "stroke_count": metrics.stroke_count,
        "lift_count": metrics.lift_count,
        "estimated_plot_seconds": round(metrics.estimated_plot_seconds, 1),
        "estimated_plot_minutes": round(metrics.estimated_plot_minutes, 2),
    }


def _optimisation_report_dict(
    report: OptimisationReport | None,
    strokes: list[PlotStroke],
    *,
    enabled: bool,
    start_point: tuple[float, float],
) -> dict[str, Any]:
    if report is None:
        timing = TimingConfig()
        metrics = measure_plot(strokes, start_point=start_point, timing=timing)
        return {
            "enabled": False,
            "algorithm": "source-order-with-semantic-buckets",
            "fallback_applied": False,
            "before": _plot_metrics_dict(metrics),
            "after": _plot_metrics_dict(metrics),
            "pen_up_saved_mm": 0.0,
            "timing_assumptions": {
                "draw_speed_mm_s": timing.draw_speed_mm_s,
                "travel_speed_mm_s": timing.travel_speed_mm_s,
                "lift_seconds": timing.lift_seconds,
                "safety_factor": timing.safety_factor,
            },
            "scope": "map paths only; furniture is listed separately by layer",
        }
    return {
        "enabled": enabled,
        "algorithm": report.algorithm,
        "fallback_applied": report.fallback_applied,
        "bucket_count": report.bucket_count,
        "reversed_stroke_count": report.reversed_stroke_count,
        "empty_stroke_count": report.empty_stroke_count,
        "before": _plot_metrics_dict(report.before),
        "after": _plot_metrics_dict(report.after),
        "pen_up_saved_mm": round(report.pen_up_saved_mm, 1),
        "timing_assumptions": {
            "draw_speed_mm_s": report.timing.draw_speed_mm_s,
            "travel_speed_mm_s": report.timing.travel_speed_mm_s,
            "lift_seconds": report.timing.lift_seconds,
            "safety_factor": report.timing.safety_factor,
        },
        "scope": "map paths only; furniture is listed separately by layer",
    }


def _document_layer_ids(root: ET.Element) -> list[str]:
    """Return emitted top-level layer IDs in physical document order."""

    result: list[str] = []
    for element in root:
        if element.tag != _svg("g"):
            continue
        group_id = element.get("id", "")
        if group_id.startswith("layer-"):
            result.append(group_id.removeprefix("layer-"))
    return result


_PATH_NUMBER = re.compile(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _path_endpoints(
    path: ET.Element,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return endpoints for the absolute M/L/C/Z path grammar we emit."""

    data = path.get("d", "")
    numbers = [float(value) for value in _PATH_NUMBER.findall(data)]
    if len(numbers) < 4 or len(numbers) % 2:
        raise MapPlotterError(
            f"Generated SVG path has no schedulable endpoints: {data[:80]!r}."
        )
    start = (numbers[0], numbers[1])
    end = start if data.rstrip().endswith("Z") else (numbers[-2], numbers[-1])
    return start, end


def _add_pen_up_schedule(
    root: ET.Element,
    sequence: list[dict[str, Any]],
) -> None:
    """Measure exact generated document-order travel for every physical pen run."""

    groups = {
        group.get("id", "").removeprefix("layer-"): group
        for group in root
        if group.tag == _svg("g") and group.get("id", "").startswith("layer-")
    }
    for pen in sequence:
        current = (0.0, 0.0)
        travel_mm = 0.0
        scheduled_paths = 0
        for layer_id in pen["layers"]:
            group = groups.get(str(layer_id))
            if group is None:
                raise MapPlotterError(
                    f"Pen schedule references missing SVG layer {layer_id!r}."
                )
            for path in group.iter(_svg("path")):
                start, end = _path_endpoints(path)
                travel_mm += hypot(start[0] - current[0], start[1] - current[1])
                current = end
                scheduled_paths += 1
        if scheduled_paths != int(pen["path_count"]):
            raise MapPlotterError(
                f"Pen {pen['pen_id']!r} schedule counted {scheduled_paths} paths, "
                f"manifest reports {pen['path_count']}."
            )
        pen["pen_up_travel_mm"] = round(travel_mm, 1)
        pen["pen_up_schedule_scope"] = (
            "exact emitted path order; includes home-to-first travel and excludes "
            "return-home and manual pen-change motion"
        )
        pen["estimated_plot_seconds_including_pen_up"] = round(
            (
                float(pen["pen_down_distance_mm"]) / 40.0
                + travel_mm / 80.0
                + int(pen["path_count"]) * 0.4
            )
            * 1.15,
            1,
        )


def _production_readiness(
    layer_stats: list[dict[str, Any]],
    *,
    pen_inventory: PenInventory | None,
    stock_id: str | None,
    stock_tone: str,
    pen_down_speed: str | None,
    physical_compilation: dict[str, Any],
    physical_resolution: dict[str, Any],
    accept_physical_conflicts: bool = False,
) -> dict[str, Any]:
    """Build a conservative, machine-readable gate for physical production."""

    emitted = [layer for layer in layer_stats if layer.get("emitted")]
    blockers: list[str] = []
    selected_pen_ids = sorted(
        {str(layer.get("pen_id")) for layer in emitted if layer.get("pen_id")}
    )
    selected_inventory_pens = (
        []
        if pen_inventory is None
        else [pen for pen in pen_inventory.pens if pen.identity in selected_pen_ids]
    )
    if pen_inventory is None:
        blockers.append("style-driven pen widths are not inventory verified")
    else:
        if pen_inventory.provenance is None:
            blockers.append("pen inventory has no recording provenance")
        if pen_inventory.stock is None:
            blockers.append("pen inventory does not identify one exact paper stock")
        else:
            if stock_id != pen_inventory.stock.id:
                blockers.append(
                    f"--stock-id must match inventory stock {pen_inventory.stock.id!r}"
                )
            if stock_tone != pen_inventory.stock.tone:
                blockers.append(
                    "--stock-tone must match inventory stock tone "
                    f"{pen_inventory.stock.tone!r}"
                )

    unmeasured = sorted(
        {
            str(layer.get("pen_id"))
            for layer in emitted
            if layer.get("calibration_state") != "measured" and layer.get("pen_id")
        }
    )
    if unmeasured:
        blockers.append(
            "selected pens have nominal/unmeasured mark widths: "
            + ", ".join(unmeasured)
        )

    measured = [
        layer for layer in emitted if layer.get("calibration_state") == "measured"
    ]
    if measured and not stock_id:
        blockers.append("--stock-id is required for measured production output")
    if stock_id:
        mismatched = sorted(
            {
                str(layer.get("pen_id"))
                for layer in measured
                if layer.get("calibration_substrate") != stock_id
            }
        )
        if mismatched:
            blockers.append(
                f"pen calibration stock does not match {stock_id!r}: "
                + ", ".join(mismatched)
            )

    missing_evidence = sorted(
        pen.identity
        for pen in selected_inventory_pens
        if pen.calibration_state == "measured" and pen.calibration is None
    )
    if missing_evidence:
        blockers.append(
            "measured pens lack ten-specimen calibration evidence: "
            + ", ".join(missing_evidence)
        )
    measured_calibrations = [
        pen.calibration
        for pen in selected_inventory_pens
        if pen.calibration_state == "measured" and pen.calibration is not None
    ]
    if measured_calibrations and not pen_down_speed:
        blockers.append(
            "--pen-down-speed is required and must match measured calibration runs"
        )
    if pen_down_speed:
        speed_mismatches = sorted(
            pen.identity
            for pen in selected_inventory_pens
            if pen.calibration is not None
            and pen.calibration.pen_down_speed != pen_down_speed
        )
        if speed_mismatches:
            blockers.append(
                f"pen calibration speed does not match {pen_down_speed!r}: "
                + ", ".join(speed_mismatches)
            )

    white_ids = sorted(
        {
            str(layer.get("pen_id"))
            for layer in emitted
            if str(layer.get("ink", "")).casefold() == "white"
        }
    )
    if white_ids and stock_tone != "dark":
        blockers.append("white ink requires --stock-tone dark: " + ", ".join(white_ids))

    dark_stock_incompatible_pen_ids = sorted(
        {
            str(layer.get("pen_id") or layer.get("id"))
            for layer in emitted
            if stock_tone == "dark"
            and str(layer.get("ink", "")).casefold() not in {"white", "gold", "silver"}
        }
    )
    if dark_stock_incompatible_pen_ids:
        blockers.append(
            "ordinary/non-opaque ink is not approved on dark stock without "
            "separate opacity evidence: " + ", ".join(dark_stock_incompatible_pen_ids)
        )

    metallic_layers = [
        layer
        for layer in emitted
        if str(layer.get("ink", "")).casefold() in {"gold", "silver"}
    ]
    allowed_metallic_roles = {"poster_title", "poster_border", "frame"}
    invalid_metallic_roles = sorted(
        {
            str(layer.get("logical_layer_id") or layer.get("id"))
            for layer in metallic_layers
            if str(layer.get("logical_layer_id") or layer.get("id"))
            not in allowed_metallic_roles
        }
    )
    if invalid_metallic_roles:
        blockers.append(
            "metallic ink is accent-only, not map linework: "
            + ", ".join(invalid_metallic_roles)
        )

    repeat_layers = sorted(
        str(layer.get("id")) for layer in emitted if int(layer.get("passes", 1)) > 1
    )
    if repeat_layers:
        blockers.append(
            "repeat passes lack a stock-specific opacity/drying calibration: "
            + ", ".join(repeat_layers)
        )

    residual_sub_nib_trail_count = int(
        physical_compilation.get("reported_physical_conflicts", 0)
    )
    if residual_sub_nib_trail_count:
        blockers.append(
            f"{residual_sub_nib_trail_count} retained trails are shorter than "
            "3 x their effective nib; use a larger sheet, tighter extent, or "
            "finer supplied pen"
        )

    conflict_scan_performed = physical_resolution.get("conflict_scan_performed") is True
    conflict_scan_truncated = physical_resolution.get("conflict_scan_truncated") is True
    conflict_scan_complete = conflict_scan_performed and not conflict_scan_truncated
    unresolved_separation_pairs_raw = physical_resolution.get(
        "below_nib_separation_pair_count"
    )
    unresolved_separation_pairs = (
        int(unresolved_separation_pairs_raw)
        if unresolved_separation_pairs_raw is not None
        else None
    )
    if not conflict_scan_performed:
        blockers.append(
            "the below-nib separation scan was not performed; rerun with "
            "--physical-audit"
        )
    elif conflict_scan_truncated:
        blockers.append(
            "the below-nib separation scan reached its safety limit and is only "
            "a lower bound"
        )
    elif unresolved_separation_pairs and not accept_physical_conflicts:
        blockers.append(
            f"{unresolved_separation_pairs} pairs of physical marks may merge; "
            "inspect the conflict report, change scale/extent/pen where needed, "
            "then explicitly use --accept-physical-conflicts after sign-off"
        )

    emitted_logical_layers = {
        str(layer.get("logical_layer_id") or layer.get("id")) for layer in emitted
    }
    ground_limits_m = {
        "roads_local": 12.0,
        "roads_other": 12.0,
        "paths": 12.0,
        "roads_major": 25.0,
    }
    ground_resolution_violations: list[dict[str, Any]] = []
    for layer in physical_resolution.get("layers", []):
        layer_id = str(layer.get("id", ""))
        if layer_id not in emitted_logical_layers or layer_id not in ground_limits_m:
            continue
        plotted_ground_width_m = float(layer.get("plotted_ground_width_m", 0.0))
        limit_m = ground_limits_m[layer_id]
        if plotted_ground_width_m > limit_m + 1e-9:
            ground_resolution_violations.append(
                {
                    "layer": layer_id,
                    "plotted_ground_width_m": round(plotted_ground_width_m, 3),
                    "maximum_ground_width_m": limit_m,
                }
            )
    if ground_resolution_violations:
        blockers.append(
            "ground-scale mark widths exceed the detail contract: "
            + ", ".join(
                f"{item['layer']} {item['plotted_ground_width_m']:g} m "
                f"> {item['maximum_ground_width_m']:g} m"
                for item in ground_resolution_violations
            )
        )

    width_fit_tolerance_violations: list[dict[str, Any]] = []
    for layer in emitted:
        requested_width_mm = float(
            layer.get("requested_width_mm", layer.get("plotted_width_mm", 0.0))
        )
        tolerance_mm = max(0.05, requested_width_mm * 0.15)
        errors = layer.get(
            "width_fit_error_options_mm", [layer.get("width_fit_error_mm", 0.0)]
        )
        violating_errors = sorted(
            float(error) for error in errors if abs(float(error)) > tolerance_mm + 1e-9
        )
        if violating_errors:
            width_fit_tolerance_violations.append(
                {
                    "layer": str(layer.get("id")),
                    "requested_width_mm": round(requested_width_mm, 3),
                    "tolerance_mm": round(tolerance_mm, 3),
                    "violating_errors_mm": [
                        round(error, 3) for error in violating_errors
                    ],
                }
            )
    compiler_width_fit_violations = int(
        physical_compilation.get("width_fit_tolerance_violation_source_strokes", 0)
    )
    if compiler_width_fit_violations or width_fit_tolerance_violations:
        blockers.append(
            "one or more achieved mark widths exceed the max(0.05 mm, 15%) "
            "fit tolerance"
        )

    return {
        "production_ready": not blockers,
        "mode": "production" if not blockers else "review-only",
        "stock_id": stock_id,
        "stock_tone": stock_tone,
        "pen_down_speed": pen_down_speed,
        "selected_pen_ids": selected_pen_ids,
        "uncalibrated_pen_ids": unmeasured,
        "dark_stock_incompatible_pen_ids": dark_stock_incompatible_pen_ids,
        "metallic_scheduled_last": bool(metallic_layers),
        "residual_sub_nib_trail_count": residual_sub_nib_trail_count,
        "conflict_scan_complete": conflict_scan_complete,
        "unresolved_below_nib_separation_pair_count": unresolved_separation_pairs,
        "physical_conflicts_accepted": bool(
            accept_physical_conflicts and unresolved_separation_pairs
        ),
        "ground_resolution_violations": ground_resolution_violations,
        "width_fit_tolerance_violation_source_strokes": (compiler_width_fit_violations),
        "width_fit_tolerance_violations": width_fit_tolerance_violations,
        "blocking_reasons": blockers,
    }


def _attach_calibration_settings(
    sequence: list[dict[str, Any]],
    pen_inventory: PenInventory | None,
) -> None:
    """Put the exact measured operating conditions in each pen-load step."""

    if pen_inventory is None:
        return
    pens_by_id = {pen.identity: pen for pen in pen_inventory.pens}
    for step in sequence:
        pen = pens_by_id.get(str(step["pen_id"]))
        if pen is None or pen.calibration is None:
            continue
        calibration = pen.calibration
        step["calibration_run_id"] = calibration.run_id
        step["calibration_stock_id"] = calibration.stock_id
        step["required_pen_down_speed"] = calibration.pen_down_speed
        step["calibration_specimen_count"] = len(calibration.specimens)
        step["calibration_coefficient_of_variation"] = round(
            calibration.coefficient_of_variation, 6
        )
        if step.get("empty"):
            step["instruction"] = (
                f"Reserve slot for {step['pen']} ({step['pen_id']}); this plate "
                "has no paths assigned and requires no physical pen load."
            )
        else:
            step["instruction"] = (
                f"Load {step['pen']} ({step['pen_id']}); use stock "
                f"{calibration.stock_id} at pen-down speed "
                f"{calibration.pen_down_speed}; plot layers: "
                f"{', '.join(step['layers'])}."
            )


def _pen_layer_setting(layer: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": layer["id"],
        "strokes": layer["strokes"],
        "passes": layer["passes"],
        "requested_width_mm": layer.get(
            "requested_width_mm", layer["plotted_width_mm"]
        ),
        "plotted_width_mm": layer["plotted_width_mm"],
        "width_fit_error_mm": layer.get("width_fit_error_mm", 0.0),
        "offset_pitch_mm": layer.get("offset_pitch_mm", 0.0),
        "width_fit_error_options_mm": layer.get(
            "width_fit_error_options_mm", [layer.get("width_fit_error_mm", 0.0)]
        ),
        "offset_pitch_options_mm": layer.get(
            "offset_pitch_options_mm", [layer.get("offset_pitch_mm", 0.0)]
        ),
        "width_fit_mode": layer.get("width_fit_mode", "style-defined"),
    }


def _pen_sequence(
    layer_stats: list[dict[str, Any]],
    document_layer_ids: list[str],
    *,
    fixed_inventory_slots: bool = False,
) -> list[dict[str, Any]]:
    """Build pen runs from actual SVG group order, coalescing adjacent pens."""

    sequence: list[dict[str, Any]] = []
    by_id = {str(layer["id"]): layer for layer in layer_stats}
    if len(by_id) != len(layer_stats):
        raise MapPlotterError("Layer statistics contain duplicate IDs.")

    for layer_id in document_layer_ids:
        try:
            layer = by_id[layer_id]
        except KeyError as exc:
            raise MapPlotterError(
                f"Emitted SVG layer {layer_id!r} has no manifest statistics."
            ) from exc
        if not layer["emitted"] or layer["path_count"] == 0:
            raise MapPlotterError(
                f"Emitted SVG layer {layer_id!r} is incorrectly marked empty."
            )
        ink_key = str(layer["ink"]).casefold()
        nominal_key = round(float(layer.get("nominal_nib_mm", layer["nib_mm"])), 6)
        pen_id = str(layer.get("pen_id") or f"{ink_key}-{_format_number(nominal_key)}")
        profile = str(layer.get("pen_profile", "style"))
        key = (profile, pen_id)
        pen = sequence[-1] if sequence and sequence[-1]["_key"] == key else None
        if pen is None:
            pen = {
                "_key": key,
                "pen": layer["pen"],
                "pen_id": pen_id,
                "pen_profile": profile,
                "ink": layer["ink"],
                "nib_mm": layer["nib_mm"],
                "nominal_nib_mm": layer.get("nominal_nib_mm", layer["nib_mm"]),
                "calibration_state": layer.get(
                    "calibration_state", "nominal-unmeasured"
                ),
                "calibration_substrate": layer.get("calibration_substrate"),
                "preview_color": layer["preview_color"],
                "layers": [],
                "layer_settings": [],
                "path_count": 0,
                "pen_down_distance_mm": 0.0,
            }
            sequence.append(pen)
        pen["layers"].append(layer["id"])
        pen["layer_settings"].append(_pen_layer_setting(layer))
        pen["path_count"] += layer["path_count"]
        pen["pen_down_distance_mm"] += layer["pen_down_distance_mm"]

    for step, pen in enumerate(sequence, start=1):
        settings = pen["layer_settings"]
        stroke_counts = sorted({int(setting["strokes"]) for setting in settings})
        pass_counts = sorted({int(setting["passes"]) for setting in settings})
        plotted_widths = sorted(
            {float(setting["plotted_width_mm"]) for setting in settings}
        )
        pen.pop("_key")
        pen["step"] = step
        pen["strokes"] = stroke_counts[0] if len(stroke_counts) == 1 else None
        pen["passes"] = pass_counts[0] if len(pass_counts) == 1 else None
        pen["plotted_width_mm"] = (
            plotted_widths[0] if len(plotted_widths) == 1 else None
        )
        pen["settings_vary_by_layer"] = any(
            len(values) > 1 for values in (stroke_counts, pass_counts, plotted_widths)
        )
        pen["pen_down_distance_mm"] = round(pen["pen_down_distance_mm"], 1)
        pen["minimum_plot_seconds"] = round(
            (pen["pen_down_distance_mm"] / 40.0 + pen["path_count"] * 0.4) * 1.15,
            1,
        )
        pen["timing_scope"] = (
            "all emitted paths in these layers; excludes pen-up travel and "
            "manual pen-change time"
        )
        pen["instruction"] = (
            f"Load {pen['pen']} ({pen['pen_id']}) and plot layers: "
            f"{', '.join(pen['layers'])}."
        )

    if fixed_inventory_slots:
        configured_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for layer in layer_stats:
            configured_by_key.setdefault(_layer_stat_pen_key(layer), []).append(layer)
        active_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for pen in sequence:
            key = (str(pen["pen_profile"]), str(pen["pen_id"]))
            if key in active_by_key:
                raise MapPlotterError(
                    f"Fixed pen slot {key!r} appears in multiple document runs."
                )
            active_by_key[key] = pen

        fixed_sequence: list[dict[str, Any]] = []
        for step, (key, configured_layers) in enumerate(
            configured_by_key.items(), start=1
        ):
            pen = active_by_key.pop(key, None)
            configured_settings = [
                _pen_layer_setting(layer) for layer in configured_layers
            ]
            configured_ids = [str(layer["id"]) for layer in configured_layers]
            if pen is None:
                exemplar = configured_layers[0]
                stroke_counts = sorted(
                    {int(setting["strokes"]) for setting in configured_settings}
                )
                pass_counts = sorted(
                    {int(setting["passes"]) for setting in configured_settings}
                )
                plotted_widths = sorted(
                    {
                        float(setting["plotted_width_mm"])
                        for setting in configured_settings
                    }
                )
                pen = {
                    "pen": exemplar["pen"],
                    "pen_id": key[1],
                    "pen_profile": key[0],
                    "ink": exemplar["ink"],
                    "nib_mm": exemplar["nib_mm"],
                    "nominal_nib_mm": exemplar.get(
                        "nominal_nib_mm", exemplar["nib_mm"]
                    ),
                    "calibration_state": exemplar.get(
                        "calibration_state", "nominal-unmeasured"
                    ),
                    "calibration_substrate": exemplar.get("calibration_substrate"),
                    "preview_color": exemplar["preview_color"],
                    "layers": [],
                    "layer_settings": [],
                    "path_count": 0,
                    "pen_down_distance_mm": 0.0,
                    "strokes": (stroke_counts[0] if len(stroke_counts) == 1 else None),
                    "passes": pass_counts[0] if len(pass_counts) == 1 else None,
                    "plotted_width_mm": (
                        plotted_widths[0] if len(plotted_widths) == 1 else None
                    ),
                    "settings_vary_by_layer": any(
                        len(values) > 1
                        for values in (stroke_counts, pass_counts, plotted_widths)
                    ),
                    "minimum_plot_seconds": 0.0,
                    "timing_scope": (
                        "empty fixed inventory slot; no paths and no physical pen load"
                    ),
                    "instruction": (
                        f"Reserve slot for {exemplar['pen']} ({key[1]}); this plate "
                        "has no paths assigned to the slot."
                    ),
                }
            pen["step"] = step
            pen["configured_layers"] = configured_ids
            pen["omitted_layers"] = [
                layer_id for layer_id in configured_ids if layer_id not in pen["layers"]
            ]
            pen["empty"] = not pen["layers"]
            pen["slot_status"] = "empty" if pen["empty"] else "active"
            fixed_sequence.append(pen)
        if active_by_key:
            raise MapPlotterError(
                "Emitted pen domains are absent from the fixed inventory slots: "
                f"{sorted(active_by_key)}."
            )
        sequence = fixed_sequence

    planned_layer_ids = [
        str(layer_id) for pen in sequence for layer_id in pen["layers"]
    ]
    if planned_layer_ids != document_layer_ids:
        raise MapPlotterError(
            "Manifest pen order does not match the emitted SVG layer order."
        )
    return sequence


def render_svg(
    output_path: Path,
    *,
    title: str,
    features: list[MapFeature],
    styles: list[LayerStyle],
    layout: Layout,
    acquisition: AcquisitionResult,
    simplify_mm: float,
    families: tuple[str, ...],
    include_frame: bool,
    include_attribution: bool = True,
    include_scale_bar: bool = True,
    external_attribution_placement: str | None = None,
    subtitle: str | None = None,
    detail_lines: tuple[str, ...] = (),
    road_style: str | None = None,
    optimise: bool = True,
    extent_fit: str = "contain",
    detail_profile: str = "plot",
    physical_audit: bool | None = None,
    pen_inventory: PenInventory | None = None,
    allowed_nibs_mm: tuple[float, ...] | None = None,
    allow_repeat_passes: bool = False,
    stock_id: str | None = None,
    stock_tone: str = "light",
    pen_down_speed: str | None = None,
    accept_physical_conflicts: bool = False,
    require_production_ready: bool = False,
    design_contract: dict[str, Any] | None = None,
    water_fill: str = "none",
    landmark_buildings: bool = False,
    course_clearance_mm: float = 0.0,
    landmark_refs: tuple[str, ...] = (),
    poster_layout: str = "classic",
    person_name: str | None = None,
    degree: str | None = None,
    honours: str | None = None,
    years: str | None = None,
    memorabilia_variant: str = "standard",
    rowing_course: Any | None = None,
    crew: Any | None = None,
) -> dict[str, Any]:
    if road_style is None:
        road_style = "centreline" if detail_profile != "plot" else "multi"
    if road_style not in ROAD_STYLE_CHOICES:
        raise MapPlotterError(
            f"Road style must be one of: {', '.join(sorted(ROAD_STYLE_CHOICES))}."
        )
    if extent_fit not in {"contain", "cover"}:
        raise MapPlotterError("Extent fit must be contain or cover.")
    if detail_profile not in DETAIL_PROFILE_CHOICES:
        raise MapPlotterError(
            "Detail profile must be one of: "
            f"{', '.join(sorted(DETAIL_PROFILE_CHOICES))}."
        )
    if poster_layout not in POSTER_LAYOUT_CHOICES:
        raise MapPlotterError(
            f"Poster layout must be one of: {', '.join(sorted(POSTER_LAYOUT_CHOICES))}."
        )
    if memorabilia_variant not in MEMORABILIA_VARIANTS:
        raise MapPlotterError(
            "Memorabilia variant must be one of: "
            + ", ".join(MEMORABILIA_VARIANTS)
            + "."
        )
    if poster_layout != "university-memorabilia" and memorabilia_variant != "standard":
        raise MapPlotterError(
            "A non-standard memorabilia variant requires the "
            "university-memorabilia poster layout."
        )
    if memorabilia_variant == "clean-personalised" and honours:
        raise MapPlotterError(
            "The clean personalised memorabilia footer has no honours cell; "
            "omit honours or use the standard variant."
        )
    if memorabilia_variant == "clean-personalised" and subtitle:
        raise MapPlotterError(
            "The clean personalised memorabilia header uses the subtitle zone "
            "for coordinates; omit the subtitle or use the standard variant."
        )
    if landmark_refs and not landmark_buildings:
        raise MapPlotterError(
            "Required landmark refs require landmark-building selection."
        )
    if poster_layout == "rowing-crew" and crew is None:
        raise MapPlotterError(
            "The rowing-crew layout needs a crew; pass --crew-file <path>."
        )
    if crew is not None and poster_layout != "rowing-crew":
        raise MapPlotterError(
            f"A crew was supplied but --poster-layout is {poster_layout!r}, which "
            "draws no boat and no names. Use --poster-layout rowing-crew, or drop "
            "the crew file."
        )
    if poster_layout == "rowing-course" and rowing_course is None:
        raise MapPlotterError(
            "The rowing-course layout needs a verified course; pass "
            "--rowing-course <id>."
        )
    if poster_layout in HEADER_COMPASS_LAYOUTS:
        if layout.preset not in A5_POSTER_PRESETS:
            raise MapPlotterError(
                f"Poster layout {poster_layout!r} requires a poster preset."
            )
        if include_scale_bar:
            raise MapPlotterError(
                f"Poster layout {poster_layout!r} uses its dedicated header "
                "compass and requires the map-overlay scale bar to be disabled."
            )
    source_faithful = detail_profile in SOURCE_COMPLETE_DETAIL_PROFILES
    full_cartography = detail_profile in FULL_CARTOGRAPHY_DETAIL_PROFILES
    ink_balanced = detail_profile in INK_BUDGETED_DETAIL_PROFILES
    if physical_audit is None:
        physical_audit = full_cartography
    if ink_balanced and layout.preset not in A5_POSTER_PRESETS:
        raise MapPlotterError(
            "The ink-balanced profile currently requires an A5 poster preset."
        )
    if ink_balanced and road_style != "centreline":
        raise MapPlotterError(
            "The ink-balanced profile requires --road-style centreline so its "
            "physical ink budget remains exact."
        )
    if stock_tone not in {"light", "mid", "dark"}:
        raise MapPlotterError("Stock tone must be light, mid, or dark.")
    if stock_id is not None and not stock_id.strip():
        raise MapPlotterError("Stock id must be non-empty text when provided.")
    stock_id = stock_id.strip() if stock_id is not None else None
    if pen_down_speed is not None and not pen_down_speed.strip():
        raise MapPlotterError("Pen-down speed must be non-empty text when provided.")
    pen_down_speed = pen_down_speed.strip() if pen_down_speed is not None else None
    if include_attribution and external_attribution_placement is not None:
        raise MapPlotterError(
            "External attribution placement cannot be set when attribution is embedded."
        )
    if not include_attribution and not (
        external_attribution_placement and external_attribution_placement.strip()
    ):
        raise MapPlotterError(
            "A recorded external attribution placement is required when visible "
            "attribution is omitted."
        )
    external_attribution_placement = (
        external_attribution_placement.strip()
        if external_attribution_placement is not None
        else None
    )
    # Publish the plate's optional column-split zones so a theme may name one.
    # This adds zones; it never moves the map field, so the projection above is
    # unaffected.
    layout = with_split_zones(layout)
    if poster_layout in CREW_LAYOUTS:
        # Done before anything is projected: the crew stack gives map height to
        # the boat and the crew list, so the map field moves and everything
        # downstream has to see the same rectangle.
        layout = with_crew_zones(layout)
    page = layout.page
    root = ET.Element(
        _svg("svg"),
        {
            "width": f"{_format_number(page.width_mm)}mm",
            "height": f"{_format_number(page.height_mm)}mm",
            "viewBox": f"0 0 {_format_number(page.width_mm)} {_format_number(page.height_mm)}",
            "version": "1.1",
        },
    )
    ET.SubElement(root, _svg("title")).text = title
    ET.SubElement(root, _svg("desc")).text = (
        "Plotter-oriented vector map generated from OpenStreetMap data. "
        "Coordinates are baked in millimetres and grouped into named Inkscape layers."
    )
    metadata = ET.SubElement(root, _svg("metadata"))
    metadata.set(f"{{{MAP_NS}}}generator", "city-map-plotter 0.2.0")
    metadata.set(f"{{{MAP_NS}}}source", acquisition.endpoint)
    metadata.set(f"{{{MAP_NS}}}license", "OpenStreetMap data: ODbL 1.0")
    metadata.set(
        f"{{{MAP_NS}}}pen-profile",
        pen_inventory.id if pen_inventory is not None else "style",
    )
    ET.SubElement(
        root,
        f"{{{SODIPODI_NS}}}namedview",
        {
            "id": "namedview-mapplot",
            "pagecolor": "#ffffff",
            "showborder": "true",
            f"{{{INKSCAPE_NS}}}document-units": "mm",
            f"{{{INKSCAPE_NS}}}showpageshadow": "2",
        },
    )

    water_dot_nib_mm = (
        _single_stroke_effective_layer_nib(
            styles,
            "waterways",
            purpose="Water stipple",
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        if water_fill == "dots"
        else 0.25
    )
    landmark_nib_mm = (
        _single_stroke_effective_layer_nib(
            styles,
            "buildings",
            purpose="Landmark outlines",
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        if landmark_buildings
        else 0.25
    )
    landmark_source_tags = _required_landmark_source_tags(
        acquisition.data, landmark_refs
    )
    # Extraction deliberately retains every supported source class so one
    # pinned snapshot can serve different styles.  Do not project and compile
    # features whose style is disabled for this plate: they cannot emit a mark,
    # and on full-city extents (especially disabled footways) they can dominate
    # memory and runtime without changing a single SVG path.  The unfiltered
    # ``features`` list remains below for the independent raw/completeness
    # audits, which already receive the explicit enabled-layer set.
    enabled_cartography_layers = {style.id for style in styles}
    cartography_features = [
        feature for feature in features if feature.layer in enabled_cartography_layers
    ]
    pre_budget_prepared = prepare_map_strokes(
        cartography_features,
        layout,
        simplify_mm=simplify_mm,
        detail_profile=detail_profile,
        water_fill=water_fill,
        landmark_buildings=landmark_buildings,
        landmark_refs=landmark_refs,
        landmark_source_tags=landmark_source_tags,
        water_dot_nib_mm=water_dot_nib_mm,
        landmark_nib_mm=landmark_nib_mm,
        course_clearance_mm=course_clearance_mm,
    )
    ink_budget_ledger: dict[str, Any] | tuple[()] = ()
    if ink_balanced:
        effective_nibs = _ink_budget_effective_nibs(
            styles,
            layout,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        budget_result = select_ink_balanced_strokes(
            pre_budget_prepared.strokes,
            styles,
            layout,
            include_frame=include_frame,
            include_north=True,
            effective_nib_by_layer=effective_nibs,
            road_style=road_style,
        )
        ink_budget_ledger = budget_result.ink_budget_gate.as_dict()
        budget_diagnostics = dict(budget_result.diagnostics)
        budget_diagnostics["profile"] = detail_profile
        budget_diagnostics["omission_ledger"] = ink_budget_ledger
        prepared_diagnostics = deepcopy(pre_budget_prepared.diagnostics)
        prepared_diagnostics["pre_budget_source_lineage"] = deepcopy(
            prepared_diagnostics.get("source_lineage", {})
        )
        prepared_diagnostics["source_lineage"] = _budgeted_source_lineage(
            pre_budget_prepared.strokes,
            budget_result.strokes,
            prepared_diagnostics["pre_budget_source_lineage"],
            ink_budget_ledger,
        )
        prepared_diagnostics["ink_budget"] = budget_diagnostics
        prepared = CartographyResult(
            strokes=budget_result.strokes,
            diagnostics=prepared_diagnostics,
            warnings=[
                *pre_budget_prepared.warnings,
                (
                    "Applied the verified ink-balanced A5 selection policy: "
                    f"retained {budget_result.diagnostics['retained_stroke_count']} "
                    "cartographic strokes and recorded "
                    f"{budget_result.diagnostics['omitted_stroke_count']} "
                    "budget-gated strokes in the manifest ledger."
                ),
            ],
        )
    else:
        prepared = pre_budget_prepared
    cartographic_lineage = prepared.diagnostics.get("source_lineage", {})
    if source_faithful and int(
        cartographic_lineage.get("unexplained_omitted_source_ref_count", 0)
    ):
        omitted = cartographic_lineage.get("unexplained_omitted_source_refs", [])
        preview = ", ".join(str(item) for item in omitted[:5])
        raise MapPlotterError(
            "Faithful cartography omitted visible source geometry"
            f" ({preview or 'see source-lineage diagnostics'})."
        )
    physical = compile_physical_strokes(
        prepared.strokes,
        styles,
        clip_rect=layout.clip_rect,
        road_style=road_style,
        preserve_network=(detail_profile in PHYSICAL_MINIMUM_GATED_DETAIL_PROFILES),
        preserve_all=detail_profile == "faithful",
        drop_residual_conflicts=(
            detail_profile in PHYSICAL_MINIMUM_GATED_DETAIL_PROFILES
        ),
        pen_inventory=pen_inventory,
        allowed_nibs_mm=allowed_nibs_mm,
        allow_repeat_passes=allow_repeat_passes,
    )
    if ink_balanced:
        physical_map_ink_mm2 = sum(
            _serialized_polyline_length(stroke) * float(stroke.tags["plot:nib-mm"])
            for stroke in physical.strokes
        )
        fixed_ink_mm2 = float(budget_diagnostics["fixed_ink_mm2"])
        final_ink_mm2 = physical_map_ink_mm2 + fixed_ink_mm2
        field_area_mm2 = float(budget_diagnostics["field_area_mm2"])
        hard_max_coverage = float(budget_diagnostics["hard_max_coverage"])
        hard_max_ink_mm2 = field_area_mm2 * hard_max_coverage
        planned_map_ink_mm2 = float(budget_diagnostics["selected_map_ink_mm2"])
        if physical_map_ink_mm2 > planned_map_ink_mm2 + 1e-6:
            raise MapPlotterError(
                "Ink-balanced physical compilation exceeded its conservative "
                "cartographic map-ink plan."
            )
        if final_ink_mm2 > hard_max_ink_mm2 + 1e-6:
            raise MapPlotterError(
                "Ink-balanced physical output exceeds the binding plate budget: "
                f"{final_ink_mm2:.3f} mm² required, "
                f"{hard_max_ink_mm2:.3f} mm² allowed."
            )
        budget_diagnostics["final_physical_verification"] = {
            "verified": True,
            "measurement": (
                "serialized 0.001 mm physical map paths times each effective "
                "nib, plus exact in-field frame and north-mark reservation"
            ),
            "physical_map_ink_mm2": round(physical_map_ink_mm2, 9),
            "fixed_ink_mm2": round(fixed_ink_mm2, 9),
            "total_ink_mm2": round(final_ink_mm2, 9),
            "field_area_mm2": round(field_area_mm2, 9),
            "coverage": round(final_ink_mm2 / field_area_mm2, 9),
            "hard_max_coverage": hard_max_coverage,
            "hard_max_ink_mm2": round(hard_max_ink_mm2, 9),
            "headroom_mm2": round(hard_max_ink_mm2 - final_ink_mm2, 9),
        }
    prepared_refs = _stroke_source_ref_set(prepared.strokes)
    physical_refs = _stroke_source_ref_set(physical.strokes)
    physically_omitted_refs = sorted(prepared_refs - physical_refs)
    physical_omission_entries = [item.as_dict() for item in physical.omissions]
    physical_omission_ledger = physical.diagnostics["physical_minimum_omission_ledger"]
    physically_evidenced_refs = list(
        physical_omission_ledger[
            "fully_omitted_source_refs_with_complete_input_evidence"
        ]
    )
    if physical_omission_ledger["fully_omitted_source_refs"] != physically_omitted_refs:
        raise MapPlotterError(
            "Physical omission ledger disagrees with source-lineage coverage."
        )
    evidenced_omitted_refs = sorted(
        set(physically_omitted_refs) & set(physically_evidenced_refs)
    )
    unevidenced_omitted_refs = sorted(
        set(physically_omitted_refs) - set(physically_evidenced_refs)
    )
    physical_lineage = {
        "prepared_source_ref_count": len(prepared_refs),
        "emitted_source_ref_count": len(prepared_refs & physical_refs),
        "omitted_source_ref_count": len(physically_omitted_refs),
        "omitted_source_refs": physically_omitted_refs,
        "minimum_omission_evidence_schema_version": 1,
        "minimum_omission_evidence_entry_count": len(physical_omission_entries),
        "minimum_omission_evidenced_source_ref_count": len(physically_evidenced_refs),
        "minimum_omission_evidenced_source_refs": physically_evidenced_refs,
        "omitted_source_refs_with_minimum_evidence": evidenced_omitted_refs,
        "omitted_source_refs_without_minimum_evidence": unevidenced_omitted_refs,
    }
    if detail_profile == "faithful" and physically_omitted_refs:
        raise MapPlotterError(
            "Faithful physical compilation omitted source geometry"
            f" ({', '.join(physically_omitted_refs[:5])})."
        )
    physical_resolution, resolution_warnings = physical_resolution_report(
        physical.strokes,
        styles,
        scale_denominator=layout.scale_denominator,
        simplify_mm=simplify_mm,
        detect_conflicts=physical_audit,
    )
    emission_styles = (
        _styles_by_physical_pen(styles) if pen_inventory is None else list(styles)
    )
    style_position = {style.id: index for index, style in enumerate(emission_styles)}
    strokes = sorted(
        physical.strokes,
        key=lambda stroke: style_position.get(stroke.layer, len(style_position)),
    )
    optimisation_report: OptimisationReport | None = None
    if optimise:
        strokes, optimisation_report = optimise_strokes(
            strokes,
            bucket_key=lambda stroke: (
                stroke.layer,
                int(stroke.tags.get("plot:pass-index", "1")),
            ),
            start_point=(layout.map_x_mm, layout.map_y_mm),
        )
    optimisation_diagnostics = _optimisation_report_dict(
        optimisation_report,
        strokes,
        enabled=optimise,
        start_point=(layout.map_x_mm, layout.map_y_mm),
    )
    cartography_diagnostics = dict(prepared.diagnostics)
    cartography_diagnostics["physical_compilation"] = physical.diagnostics
    cartography_diagnostics["physical_source_lineage"] = physical_lineage
    cartography_diagnostics["physical_resolution"] = physical_resolution
    cartography_diagnostics["plot_optimisation"] = optimisation_diagnostics
    cartography_warnings = [
        *prepared.warnings,
        *physical.warnings,
        *resolution_warnings,
    ]

    # Course labels are laid out before the map is measured, and the linework
    # under each one is cut away here -- a plotter cannot draw a white halo on
    # white paper, so the halo has to be an absence of ink. Doing it now means
    # the manifest, the path counts and the ink all describe what is drawn.
    course_label_plan = None
    label_knockout_diagnostics: dict[str, Any] = {"applied": False}
    if rowing_course is not None:
        course_label_plan = plan_course_labels(
            layout,
            course=rowing_course,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        strokes, label_knockout_diagnostics = knock_out_labels(
            strokes,
            course_label_plan.knockouts,
            minimum_length_mm=3 * course_label_plan.nib_mm,
            close_mm=course_label_plan.cap_mm * 0.42,
        )
    cartography_diagnostics["course_label_knockout"] = label_knockout_diagnostics

    by_layer: dict[str, list[PlotStroke]] = {style.id: [] for style in styles}
    for stroke in strokes:
        if stroke.layer in by_layer:
            by_layer[stroke.layer].append(stroke)

    layer_stats: list[dict[str, Any]] = []
    group_index = 0
    for style in emission_styles:
        assert style.ink is not None and style.nib_mm is not None
        all_layer_strokes = by_layer[style.id]
        variants: dict[tuple[str, str, float, float], list[PlotStroke]] = {}
        for stroke in all_layer_strokes:
            ink = stroke.tags.get("plot:ink", style.ink)
            effective_nib = float(stroke.tags.get("plot:nib-mm", style.nib_mm))
            nominal_nib = float(stroke.tags.get("plot:nominal-nib-mm", effective_nib))
            pen_id = stroke.tags.get(
                "plot:pen-id",
                f"{ink.casefold()}-{nominal_nib:g}",
            )
            variants.setdefault((pen_id, ink, nominal_nib, effective_nib), []).append(
                stroke
            )

        if not variants:
            empty_plan = (
                fit_pen_width(
                    pen_inventory,
                    ink=style.ink,
                    requested_width_mm=style.plotted_width_mm,
                    allowed_nibs_mm=allowed_nibs_mm,
                )
                if pen_inventory is not None
                else style_pen_width(
                    ink=style.ink,
                    nib_mm=style.nib_mm,
                    stroke_count=style.strokes,
                )
            )
            layer_stats.append(
                _layer_stats(
                    layer_id=style.id,
                    logical_layer_id=style.id,
                    label=style.label,
                    pen=empty_plan.pen.label,
                    ink=empty_plan.pen.ink,
                    nib_mm=empty_plan.pen.mark_width_mm,
                    nominal_nib_mm=empty_plan.pen.nominal_nib_mm,
                    requested_width_mm=empty_plan.requested_width_mm,
                    width_fit_mode=empty_plan.mode,
                    pen_profile=(
                        pen_inventory.id if pen_inventory is not None else "style"
                    ),
                    pen_id=empty_plan.pen.id,
                    calibration_state=empty_plan.pen.calibration_state,
                    calibration_substrate=empty_plan.pen.substrate,
                    strokes=empty_plan.stroke_count,
                    passes=style.passes,
                    color=style.stroke,
                    plotted_width_mm=empty_plan.plotted_width_mm,
                    path_count=0,
                    length_mm=0.0,
                    emitted=False,
                    svg_group_id=None,
                    svg_layer_label=None,
                )
            )
            continue

        multiple_variants = len(variants) > 1
        for variant_number, (variant_key, layer_strokes) in enumerate(
            sorted(
                variants.items(),
                key=lambda item: (
                    item[0][1].casefold(),
                    item[0][2],
                    item[0][0],
                ),
            ),
            start=1,
        ):
            pen_id, ink, nominal_nib, effective_nib = variant_key
            group_index += 1
            variant_id = (
                style.id
                if not multiple_variants
                else f"{style.id}--pen-{variant_number:02d}"
            )
            group_id = f"layer-{variant_id}"
            pen_label = f"{ink} {nominal_nib:g}"
            layer_label = f"{group_index:02d} — {style.label} — {pen_label}"
            observed_stroke_counts = sorted(
                {
                    int(stroke.tags.get("plot:stroke-count", style.strokes))
                    for stroke in layer_strokes
                }
            )
            observed_plotted_widths = sorted(
                {
                    float(
                        stroke.tags.get("plot:plotted-width-mm", style.plotted_width_mm)
                    )
                    for stroke in layer_strokes
                }
            )
            requested_widths = sorted(
                {
                    float(
                        stroke.tags.get(
                            "plot:requested-width-mm", style.plotted_width_mm
                        )
                    )
                    for stroke in layer_strokes
                }
            )
            fit_errors = sorted(
                {
                    float(stroke.tags.get("plot:width-fit-error-mm", 0.0))
                    for stroke in layer_strokes
                }
            )
            offset_pitches = sorted(
                {
                    float(stroke.tags.get("plot:offset-pitch-mm", 0.0))
                    for stroke in layer_strokes
                }
            )
            fit_modes = sorted(
                {
                    stroke.tags.get("plot:width-fit-mode", "style-defined")
                    for stroke in layer_strokes
                }
            )
            calibration_states = sorted(
                {
                    stroke.tags.get("plot:calibration-state", "nominal-unmeasured")
                    for stroke in layer_strokes
                }
            )
            substrates = sorted(
                {
                    stroke.tags["plot:calibration-substrate"]
                    for stroke in layer_strokes
                    if "plot:calibration-substrate" in stroke.tags
                }
            )
            effective_strokes = max(observed_stroke_counts)
            effective_width_mm = max(observed_plotted_widths)
            group = ET.SubElement(
                root,
                _svg("g"),
                {
                    "id": group_id,
                    f"{{{INKSCAPE_NS}}}groupmode": "layer",
                    f"{{{INKSCAPE_NS}}}label": layer_label,
                    "fill": "none",
                    "stroke": style.stroke,
                    "stroke-width": _format_measurement(effective_nib),
                    "stroke-linecap": "round",
                    "stroke-linejoin": "round",
                    **_physical_group_attributes(
                        ink=ink,
                        nib_mm=effective_nib,
                        nominal_nib_mm=nominal_nib,
                        strokes=effective_strokes,
                        passes=style.passes,
                        plotted_width_mm=effective_width_mm,
                        requested_width_mm=max(requested_widths),
                        width_fit_error_mm=(
                            fit_errors[0]
                            if len(fit_errors) == 1
                            else max(fit_errors, key=abs)
                        ),
                        offset_pitch_mm=(
                            offset_pitches[0]
                            if len(offset_pitches) == 1
                            else max(offset_pitches)
                        ),
                        width_fit_mode=(
                            fit_modes[0] if len(fit_modes) == 1 else "varies"
                        ),
                        pen_profile=(
                            pen_inventory.id if pen_inventory is not None else "style"
                        ),
                        pen_id=pen_id,
                        calibration_state=(
                            calibration_states[0]
                            if len(calibration_states) == 1
                            else "varies"
                        ),
                        calibration_substrate=(
                            substrates[0] if len(substrates) == 1 else None
                        ),
                    ),
                },
            )
            ET.SubElement(group, _svg("title")).text = (
                f"Pen: {pen_label}; nominal nib: {nominal_nib:g} mm; "
                f"effective mark: {effective_nib:g} mm; plotted width: "
                f"up to {effective_width_mm:g} mm"
            )
            length_mm = 0.0
            for stroke in layer_strokes:
                attributes = {
                    "d": _path_data(
                        stroke.points,
                        smooth=stroke.smooth,
                        bounds=layout.clip_rect,
                    ),
                    "data-osm-type": stroke.osm_type,
                    "data-osm-id": stroke.osm_id,
                    "data-part": stroke.part,
                }
                if stroke.smooth:
                    attributes["data-bezier-smoothed"] = "true"
                for tag_name in (
                    "highway",
                    "construction",
                    "water",
                    "waterway",
                    "railway",
                    "bridge",
                    "tunnel",
                    "layer",
                    "z-layer",
                    "access",
                    "compiled",
                    "lanes",
                    "width",
                    "road-rank",
                    "source-count",
                    "source-refs",
                    "represented-centreline-source-refs",
                    "generalization",
                    "rounding",
                    "rounding-tolerance-mm",
                    "rounding-error-mm",
                ):
                    if tag_name in stroke.tags:
                        attributes[f"data-osm-{tag_name}"] = stroke.tags[tag_name]
                if "mapplot:area-role" in stroke.tags:
                    attributes["data-area-role"] = stroke.tags["mapplot:area-role"]
                for tag_name in (
                    "water-pattern",
                    "water-dot-spacing-mm",
                    "water-dot-diameter-mm",
                    "landmark-role",
                    "landmark-rank",
                    "projected-area-mm2",
                ):
                    mapplot_tag = f"mapplot:{tag_name}"
                    if mapplot_tag in stroke.tags:
                        attributes[f"data-mapplot-{tag_name}"] = stroke.tags[
                            mapplot_tag
                        ]
                    elif tag_name in stroke.tags:
                        attributes[f"data-mapplot-{tag_name}"] = stroke.tags[tag_name]
                if "area:highway" in stroke.tags:
                    attributes["data-osm-area-highway"] = stroke.tags["area:highway"]
                attributes.update(_plot_path_attributes(stroke.tags))
                path = ET.SubElement(group, _svg("path"), attributes)
                if stroke.name:
                    ET.SubElement(path, _svg("title")).text = stroke.name
                length_mm += polyline_length(stroke.points)
            layer_stats.append(
                _layer_stats(
                    layer_id=variant_id,
                    logical_layer_id=style.id,
                    label=style.label,
                    pen=pen_label,
                    ink=ink,
                    nib_mm=effective_nib,
                    nominal_nib_mm=nominal_nib,
                    requested_width_mm=max(requested_widths),
                    width_fit_error_mm=(
                        fit_errors[0]
                        if len(fit_errors) == 1
                        else max(fit_errors, key=abs)
                    ),
                    offset_pitch_mm=(
                        offset_pitches[0]
                        if len(offset_pitches) == 1
                        else max(offset_pitches)
                    ),
                    width_fit_mode=(fit_modes[0] if len(fit_modes) == 1 else "varies"),
                    pen_profile=(
                        pen_inventory.id if pen_inventory is not None else "style"
                    ),
                    pen_id=pen_id,
                    calibration_state=(
                        calibration_states[0]
                        if len(calibration_states) == 1
                        else "varies"
                    ),
                    calibration_substrate=(
                        substrates[0] if len(substrates) == 1 else None
                    ),
                    strokes=effective_strokes,
                    passes=style.passes,
                    color=style.stroke,
                    plotted_width_mm=effective_width_mm,
                    path_count=len(layer_strokes),
                    length_mm=length_mm,
                    emitted=True,
                    svg_group_id=group_id,
                    svg_layer_label=layer_label,
                    stroke_count_options=observed_stroke_counts,
                    plotted_width_options_mm=observed_plotted_widths,
                    width_fit_error_options_mm=fit_errors,
                    offset_pitch_options_mm=offset_pitches,
                )
            )

    if sum(layer["path_count"] for layer in layer_stats) == 0 and not landmark_refs:
        raise MapPlotterError(
            "Features were loaded, but none intersect the requested bounding box. "
            "Check that the selected JSON/PBF input covers this extent and that "
            "the coordinates are in the right order."
        )

    furniture_policy = (
        furniture_policy_from_contract(design_contract)
        if design_contract is not None
        else default_furniture_policy(layout)
        if layout.preset in A5_POSTER_PRESETS
        else None
    )

    if include_frame:
        append_map_frame(
            root,
            layout,
            layer_stats,
            policy=furniture_policy or FurniturePolicy(),
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )

    if poster_layout not in HEADER_COMPASS_LAYOUTS:
        append_map_furniture(
            root,
            layout,
            layer_stats,
            policy=furniture_policy or FurniturePolicy(),
            include_scale_bar=include_scale_bar,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )

    course_evidence: dict[str, Any] | None = None
    label_evidence: dict[str, Any] | None = None
    if rowing_course is not None:
        course_evidence = append_race_course(
            root,
            layout,
            layer_stats,
            course=rowing_course,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
        label_evidence = append_course_labels(
            root,
            layout,
            layer_stats,
            course=rowing_course,
            label_plan=course_label_plan,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )

    crew_evidence: dict[str, Any] | None = None
    if poster_layout in CREW_LAYOUTS:
        assert crew is not None
        crew_evidence = append_rowing_crew_copy(
            root,
            layout,
            crew=crew,
            course=rowing_course,
            layer_stats=layer_stats,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
    elif layout.preset in A5_POSTER_PRESETS:
        append_poster_decoration(
            root,
            layout,
            title=title,
            subtitle=subtitle,
            detail_lines=detail_lines,
            poster_layout=poster_layout,
            person_name=person_name,
            degree=degree,
            honours=honours,
            years=years,
            memorabilia_variant=memorabilia_variant,
            rowing_course=rowing_course,
            layer_stats=layer_stats,
            policy=furniture_policy,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )

    if include_attribution:
        append_attribution(
            root,
            layout,
            layer_stats,
            policy=furniture_policy,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
        )
    typography_evidence = None
    if design_contract is not None:
        _validate_resolved_theme_layers(design_contract, layer_stats, root)
        _bind_design_contract_groups(root, design_contract)
        typography_evidence = _typography_evidence(root, design_contract, layout)
    unmeasured_layer_pen_ids = sorted(
        {
            str(layer.get("pen_id"))
            for layer in layer_stats
            if layer.get("emitted")
            and layer.get("calibration_state") == "nominal-unmeasured"
            and layer.get("pen_id")
        }
    )
    if pen_inventory is not None and unmeasured_layer_pen_ids:
        warning = (
            "PRODUCTION CALIBRATION REQUIRED: selected pens use nominal, unmeasured "
            f"mark widths ({', '.join(unmeasured_layer_pen_ids)}). Plot the pen "
            "calibration card and supply measured effective widths for the intended "
            "paper before production."
        )
        if warning not in cartography_warnings:
            cartography_warnings.append(warning)
    fixed_inventory_slots = poster_layout in {"city-map", "university-memorabilia"}
    _reorder_document_layers_by_pen(
        root,
        layer_stats,
        fixed_inventory_slots=fixed_inventory_slots,
    )

    # A local PBF is streamed directly into canonical features and deliberately
    # does not retain an Overpass-shaped raw object list.  Passing its synthetic
    # empty ``elements`` value here would turn "not audited" into a vacuous
    # complete=True claim.  PBF fidelity is evidenced separately by the
    # canonical-feature hashes and the cartographic/physical lineage ledgers.
    highway_audit_data = acquisition.data if acquisition.features is None else {}
    highway_audit = audit_highway_completeness(
        highway_audit_data,
        layout=layout,
        enabled_layers={style.id for style in styles},
        features=cartography_features,
        cartographic_strokes=prepared.strokes,
        physical_strokes=physical.strokes,
        physical_omission_evidence=physical_omission_entries,
        pre_budget_cartographic_strokes=(
            pre_budget_prepared.strokes if ink_balanced else None
        ),
        ink_budget_omission_evidence=(ink_budget_ledger if ink_balanced else None),
        ink_budget_diagnostics_evidence=(budget_diagnostics if ink_balanced else None),
        label_knockout_omission_evidence=label_knockout_diagnostics,
        svg=root,
        detail_profile=detail_profile,
        source_query=acquisition.query,
        include_disabled_layers=False,
    )
    raw_evidence_kind = (
        "pbf_canonical_features"
        if acquisition.features is not None
        else "cached_overpass_json_with_query"
        if acquisition.query is not None and acquisition.from_cache
        else "live_overpass_json_with_query"
        if acquisition.query is not None
        else "saved_overpass_json_without_query"
    )
    raw_geometry_audit = audit_raw_geometry_integrity(
        highway_audit_data,
        layout=layout,
        enabled_layers={style.id for style in styles},
        features=cartography_features,
        source_query=acquisition.query,
        evidence_kind=raw_evidence_kind,
    )
    raw_geometry_integrity = raw_geometry_audit.as_dict()
    raw_geometry_warnings = (
        [
            "RAW GEOMETRY INTEGRITY WARNING: "
            f"{len(raw_geometry_audit.failures)} selected source geometry "
            "failure(s) were detected; plot profile continued, so inspect "
            "rendering.raw_geometry_integrity before plotting."
        ]
        if raw_geometry_audit.failures
        else []
    )
    highway_completeness = highway_audit.as_dict(include_records=False)
    physical_evidence_audit = highway_completeness["physical_minimum_omission_evidence"]
    verified_minimum_refs = set(physical_evidence_audit["evidenced_source_refs"]) & set(
        physically_evidenced_refs
    )
    omitted_refs_with_verified_evidence = sorted(
        set(physically_omitted_refs) & verified_minimum_refs
    )
    unevidenced_physical_refs = sorted(
        set(physically_omitted_refs) - verified_minimum_refs
    )
    physical_lineage.update(
        {
            "verified_minimum_omission_evidenced_source_refs": sorted(
                verified_minimum_refs
            ),
            "omitted_source_refs_with_verified_minimum_evidence": (
                omitted_refs_with_verified_evidence
            ),
            "omitted_source_refs_without_verified_minimum_evidence": (
                unevidenced_physical_refs
            ),
        }
    )
    in_frame_deltas = [
        record
        for record in highway_audit.records
        if record.in_frame and record.status != "emitted"
    ]
    delta_preview_limit = 100
    highway_completeness["in_frame_delta_count"] = len(in_frame_deltas)
    highway_completeness["in_frame_deltas_truncated"] = (
        len(in_frame_deltas) > delta_preview_limit
    )
    highway_completeness["in_frame_delta_preview_limit"] = delta_preview_limit
    highway_completeness["in_frame_deltas"] = [
        record.as_dict() for record in in_frame_deltas[:delta_preview_limit]
    ]
    if (
        full_cartography
        and acquisition.features is None
        and not raw_geometry_audit.source_available
    ):
        raise MapPlotterError(
            "Full-cartography raw geometry integrity audit requires an "
            "Overpass-shaped "
            "elements list for JSON input."
        )
    if full_cartography and raw_geometry_audit.failures:
        preview = ", ".join(
            f"{finding.source_ref}/{finding.component} ({finding.reason})"
            for finding in raw_geometry_audit.failures[:5]
        )
        raise MapPlotterError(
            "Full-cartography raw geometry integrity audit found corrupt or "
            "missing "
            f"selected source geometry ({preview})."
        )
    if full_cartography and physical_evidence_audit["invalid_entry_count"]:
        invalid_preview = ", ".join(
            f"{item['omission_id']} ({item['reason']})"
            for item in physical_evidence_audit["invalid_entries"][:5]
        )
        raise MapPlotterError(
            "Full-cartography physical minimum omission evidence failed "
            "verification "
            f"({invalid_preview})."
        )
    label_knockout_evidence = highway_completeness["course_label_knockout_evidence"]
    if full_cartography and label_knockout_evidence["invalid_entry_count"]:
        invalid_preview = ", ".join(
            f"{item['entry']} ({item['reason']})"
            for item in label_knockout_evidence["invalid_entries"][:5]
        )
        raise MapPlotterError(
            "Course-label knockout omission evidence failed independent "
            "verification "
            f"({invalid_preview})."
        )
    if (
        detail_profile in PHYSICAL_MINIMUM_GATED_DETAIL_PROFILES
        and unevidenced_physical_refs
    ):
        raise MapPlotterError(
            "Minimum-gated physical compilation omitted source geometry "
            "without verified minimum-gate evidence"
            f" ({', '.join(unevidenced_physical_refs[:5])})."
        )
    if ink_balanced:
        budget_evidence_audit = highway_completeness["ink_budget_omission_evidence"]
        budget_policy_audit = highway_completeness["ink_budget_policy"]
        if not budget_evidence_audit.get("ledger_valid"):
            invalid_preview = ", ".join(
                f"{item.get('omission_id')} ({item.get('reason')})"
                for item in budget_evidence_audit.get("invalid_entries", [])[:5]
            )
            raise MapPlotterError(
                "Ink-budget omission evidence failed independent verification"
                f" ({invalid_preview or 'invalid ledger'})."
            )
        if not budget_policy_audit.get("policy_conformant"):
            diagnostic_preview = ", ".join(
                f"{item.get('component')} ({item.get('reason')})"
                for item in budget_evidence_audit.get("invalid_diagnostics", [])[:5]
            )
            unexpected_preview = ", ".join(
                str(item)
                for item in budget_policy_audit.get(
                    "unexpected_cartographic_drop_source_refs", []
                )[:5]
            )
            detail = diagnostic_preview or unexpected_preview or "incomplete evidence"
            raise MapPlotterError(
                "Ink-balanced highway omissions do not conform to the verified "
                f"budget policy ({detail})."
            )
    unexpected_highway_omissions = [
        record
        for record in highway_audit.missing_expected
        if record.reason
        not in {"physical_minimum_gate", "ink_budget_gate", "course_label_knockout"}
    ]
    if full_cartography and unexpected_highway_omissions:
        preview = ", ".join(
            f"{record.source_ref} ({record.reason})"
            for record in unexpected_highway_omissions[:5]
        )
        raise MapPlotterError(
            "Full-cartography highway completeness audit found source geometry "
            "missing "
            f"from the SVG ({preview})."
        )

    must_have = cartography_diagnostics.get("landmark_buildings", {}).get("must_have")
    if not isinstance(must_have, dict):
        raise MapPlotterError(
            "Cartography did not return the required-landmark disposition ledger."
        )
    dispositions = must_have.get("dispositions")
    if not isinstance(dispositions, list):
        raise MapPlotterError(
            "Required-landmark disposition ledger has no dispositions list."
        )
    pre_budget_refs = _stroke_source_ref_set(pre_budget_prepared.strokes)
    svg_paths = list(root.iter(_svg("path")))
    svg_ref_sets = [
        {
            source_ref
            for source_ref in path.get("data-osm-source-refs", "").split(";")
            if source_ref
        }
        for path in svg_paths
    ]
    verified_minimum_ref_set = set(omitted_refs_with_verified_evidence)
    for disposition in dispositions:
        if not isinstance(disposition, dict):
            raise MapPlotterError(
                "Required-landmark disposition ledger contains a non-object entry."
            )
        required_ref = str(disposition.get("requested_ref", ""))
        pre_budget_exact_refs = sorted(
            source_ref
            for source_ref in pre_budget_refs
            if _source_object_ref(source_ref) == required_ref
        )
        prepared_exact_refs = sorted(
            source_ref
            for source_ref in prepared_refs
            if _source_object_ref(source_ref) == required_ref
        )
        physical_exact_refs = sorted(
            source_ref
            for source_ref in physical_refs
            if _source_object_ref(source_ref) == required_ref
        )
        svg_exact_refs = sorted(
            {
                source_ref
                for path_refs in svg_ref_sets
                for source_ref in path_refs
                if _source_object_ref(source_ref) == required_ref
            }
        )
        physical_path_count = sum(
            any(
                _source_object_ref(source_ref) == required_ref
                for source_ref in stroke.tags.get("source-refs", "").split(";")
                if source_ref
            )
            for stroke in physical.strokes
        )
        svg_path_count = sum(
            any(
                _source_object_ref(source_ref) == required_ref
                for source_ref in path_refs
            )
            for path_refs in svg_ref_sets
        )
        disposition.update(
            pre_budget_source_refs=pre_budget_exact_refs,
            prepared_source_refs=prepared_exact_refs,
            physical_source_refs=physical_exact_refs,
            physical_path_count=physical_path_count,
            svg_source_refs=svg_exact_refs,
            svg_path_count=svg_path_count,
        )
        if disposition.get("status") != "selected":
            continue
        if not pre_budget_exact_refs:
            disposition.update(
                status="budget_omitted",
                reason=(
                    "required landmark reservation produced no final "
                    "cartographic outline"
                ),
            )
        elif not prepared_exact_refs:
            disposition.update(
                status="budget_omitted",
                reason="required landmark was omitted by the final ink budget",
            )
        elif not physical_exact_refs or physical_path_count == 0:
            verified_drop = bool(prepared_exact_refs) and all(
                source_ref in verified_minimum_ref_set
                for source_ref in prepared_exact_refs
            )
            disposition.update(
                status="physically_dropped",
                reason=(
                    "physical_minimum_gate: every prepared outline has "
                    "independently verified minimum-gate evidence"
                    if verified_drop
                    else (
                        "unexpected_physical_drop: prepared required landmark "
                        "has no final physical path"
                    )
                ),
                physical_drop_reason=(
                    "physical_minimum_gate"
                    if verified_drop
                    else "unexpected_physical_drop"
                ),
            )
        elif not svg_exact_refs or svg_path_count == 0:
            disposition.update(
                status="physically_dropped",
                reason=(
                    "unexpected_physical_drop: physical required-landmark "
                    "lineage is absent from the completed SVG"
                ),
                physical_drop_reason="unexpected_physical_drop",
            )

    must_have["all_selected"] = all(
        isinstance(disposition, dict) and disposition.get("status") == "selected"
        for disposition in dispositions
    )
    must_have["all_physical"] = all(
        isinstance(disposition, dict)
        and disposition.get("status") == "selected"
        and int(disposition.get("physical_path_count", 0)) > 0
        and int(disposition.get("svg_path_count", 0)) > 0
        for disposition in dispositions
    )
    must_have["fail_closed_before_output"] = True
    if not must_have["all_selected"] or not must_have["all_physical"]:
        status_summary = "; ".join(
            f"{disposition.get('requested_ref')}={disposition.get('status')}"
            f" ({disposition.get('reason')})"
            for disposition in dispositions
            if isinstance(disposition, dict)
        )
        raise MapPlotterError(
            "Required landmark selection failed before output publication: "
            + status_summary
        )

    generated_at = _generation_timestamp()
    source_timestamp = None
    osm3s = acquisition.data.get("osm3s")
    if isinstance(osm3s, dict):
        source_timestamp = osm3s.get("timestamp_osm_base")
    if source_timestamp is None:
        source_timestamp = acquisition.source_metadata.get("source_timestamp")

    source_manifest: dict[str, Any] = {
        "provider": "OpenStreetMap contributors",
        "license": "ODbL 1.0",
        "attribution": "Map data © OpenStreetMap contributors — https://www.openstreetmap.org/copyright",
        "endpoint": acquisition.endpoint,
        "timestamp": source_timestamp,
        "cache_path": acquisition.cache_path,
        "from_cache": acquisition.from_cache,
    }
    if acquisition.source_metadata:
        source_manifest["provenance"] = acquisition.source_metadata

    document_layer_ids = _document_layer_ids(root)
    pen_sequence = _pen_sequence(
        layer_stats,
        document_layer_ids,
        fixed_inventory_slots=fixed_inventory_slots,
    )
    _attach_calibration_settings(pen_sequence, pen_inventory)
    _add_pen_up_schedule(root, pen_sequence)
    production_readiness = _production_readiness(
        layer_stats,
        pen_inventory=pen_inventory,
        stock_id=stock_id,
        stock_tone=stock_tone,
        pen_down_speed=pen_down_speed,
        physical_compilation=physical.diagnostics,
        physical_resolution=physical_resolution,
        accept_physical_conflicts=accept_physical_conflicts,
    )
    if require_production_ready and not production_readiness["production_ready"]:
        raise MapPlotterError(
            "Production export is blocked: "
            + "; ".join(production_readiness["blocking_reasons"])
            + ". Generate a review SVG without --production, or resolve every gate."
        )
    header_separator = (
        str(layout_plate_format(layout)["city_header"]["coordinate_separator"])
        if poster_layout in {"city-map", "university-memorabilia"} else " / "
    )
    memorabilia_coordinates = coordinate_label(layout, separator=header_separator)
    if memorabilia_variant != "standard":
        raw_variants = layout_plate_format(layout).get("memorabilia_variants")
        if not isinstance(raw_variants, dict):
            raise MapPlotterError(
                "The binding plate has no personalised memorabilia variants."
            )
        raw_variant = raw_variants.get(memorabilia_variant)
        if not isinstance(raw_variant, dict) or not isinstance(
            raw_variant.get("header"), dict
        ):
            raise MapPlotterError(
                f"The binding plate has no {memorabilia_variant!r} header."
            )
        separator = str(raw_variant["header"].get("coordinate_separator", " / "))
        memorabilia_coordinates = coordinate_label(layout, separator=separator)

    manifest: dict[str, Any] = {
        "schema_version": 2,
        "generator": "city-map-plotter 0.2.0",
        "generated_at": generated_at,
        "title": title,
        "subtitle": subtitle,
        "details": list(detail_lines),
        **(
            {
                "memorabilia": {
                    "layout": poster_layout,
                    "variant": memorabilia_variant,
                    "coordinates": memorabilia_coordinates,
                    "personalisation": {
                        "person_name": person_name or "",
                        "degree": degree or "",
                        "honours": honours or "",
                        "years": years or "",
                    },
                    "blank_template": not any((person_name, degree, honours, years)),
                    "display_font": display_font_contract(),
                }
            }
            if poster_layout == "university-memorabilia"
            else {}
        ),
        **(
            {
                "city_map": {
                    "layout": poster_layout,
                    "coordinates": memorabilia_coordinates,
                    "visible_copy": [title, memorabilia_coordinates, "N"],
                    "display_font": display_font_contract(),
                }
            }
            if poster_layout == "city-map"
            else {}
        ),
        "source": source_manifest,
        "extent_wgs84": layout.bbox.as_dict(),
        "families": list(families),
        "page": {
            "paper": page.name,
            "orientation": page.orientation,
            "width_mm": page.width_mm,
            "height_mm": page.height_mm,
            "margin_mm": layout.margin_mm,
            "map_bounds_mm": {
                "x": round(layout.map_x_mm, 3),
                "y": round(layout.map_y_mm, 3),
                "width": round(layout.map_width_mm, 3),
                "height": round(layout.map_height_mm, 3),
            },
            "zones_mm": {name: zone.as_dict() for name, zone in layout.zones.items()},
        },
        "projection": {
            "name": "local equirectangular",
            "center_latitude": layout.bbox.center[0],
            "center_longitude": layout.bbox.center[1],
            "approximate_scale_denominator": round(layout.scale_denominator),
        },
        # The course is a factual claim about the world, so its provenance and
        # the measurement that accepted it travel with the plate, not just the
        # line.
        **({"race_course": course_evidence} if course_evidence is not None else {}),
        **({"crew": crew_evidence} if crew_evidence is not None else {}),
        **(
            {"course_labels": label_evidence}
            if label_evidence is not None
            else {}
        ),
        "rendering": {
            "preset": layout.preset,
            "poster_layout": poster_layout,
            "memorabilia_variant": memorabilia_variant,
            "detail_profile": detail_profile,
            "water_fill": water_fill,
            "landmark_buildings": landmark_buildings,
            "landmark_refs": list(landmark_refs),
            "road_style": road_style,
            "extent_fit": extent_fit,
            "travel_optimisation_enabled": optimise,
            "physical_conflict_audit_enabled": physical_audit,
            "repeat_passes_explicitly_approved": allow_repeat_passes,
            "production_requested": require_production_ready,
            "visible_attribution": include_attribution,
            "attribution_mode": "embedded" if include_attribution else "external",
            "external_attribution_placement": external_attribution_placement,
            "scale_bar": include_scale_bar,
            "north_mark": True,
            "stock_id": stock_id,
            "stock_tone": stock_tone,
            "pen_down_speed": pen_down_speed,
            "pen_profile": (pen_inventory.id if pen_inventory is not None else "style"),
            "pen_inventory": (
                pen_inventory.as_dict() if pen_inventory is not None else None
            ),
            "allowed_nominal_nibs_mm": (
                list(allowed_nibs_mm) if allowed_nibs_mm is not None else None
            ),
            "simplify_tolerance_mm": simplify_mm,
            "geometry_is_clipped": True,
            **({"ink_budget": budget_diagnostics} if ink_balanced else {}),
            "raw_geometry_integrity": raw_geometry_integrity,
            "highway_completeness": highway_completeness,
            "cartographic_cleanup": cartography_diagnostics,
            "document_layer_order": document_layer_ids,
            "empty_layers_omitted_from_svg": [
                layer["id"] for layer in layer_stats if not layer["emitted"]
            ],
        },
        "layers": layer_stats,
        "pen_sequence": pen_sequence,
        "production_readiness": production_readiness,
        **(
            {
                "design_contract": design_contract,
                "typography_evidence": typography_evidence,
            }
            if design_contract is not None
            else {}
        ),
        "plot_summary": {
            "inventory_pen_slots": len(pen_sequence),
            "physical_pen_steps": sum(
                not bool(step.get("empty", False)) for step in pen_sequence
            ),
            "pen_changes": max(
                0,
                sum(not bool(step.get("empty", False)) for step in pen_sequence) - 1,
            ),
            "pen_down_path_count": sum(
                int(step["path_count"]) for step in pen_sequence
            ),
            "pen_down_distance_mm": round(
                sum(float(step["pen_down_distance_mm"]) for step in pen_sequence),
                1,
            ),
            "pen_up_travel_mm": round(
                sum(float(step["pen_up_travel_mm"]) for step in pen_sequence),
                1,
            ),
            "estimated_plot_seconds_including_pen_up": round(
                sum(
                    float(step["estimated_plot_seconds_including_pen_up"])
                    for step in pen_sequence
                ),
                1,
            ),
            "lower_bound_seconds_excluding_all_pen_up_travel": round(
                sum(float(step["minimum_plot_seconds"]) for step in pen_sequence),
                1,
            ),
            "lower_bound_timing_scope": (
                "all emitted paths; includes nominal drawing and one lift cycle "
                "per path, but excludes all pen-up travel and manual pen-change time"
            ),
            "lower_bound_timing_assumptions": {
                "draw_speed_mm_s": 40.0,
                "lift_seconds_per_path": 0.4,
                "safety_factor": 1.15,
            },
            "map_path_optimisation": optimisation_diagnostics,
        },
        "warnings": [
            *(
                []
                if production_readiness["production_ready"]
                else [
                    "REVIEW OUTPUT ONLY — production gates are unresolved: "
                    + "; ".join(production_readiness["blocking_reasons"])
                ]
            ),
            "The manifest records the physical ink, nib, parallel-stroke count, pass count, and planned plotted width for every layer.",
            (
                "The attribution is single-line vector lettering and is included in the pen plan."
                if include_attribution
                else "Visible attribution was omitted by explicit export request. OpenStreetMap attribution remains required for public distribution at the recorded external placement."
            ),
            "OpenStreetMap data must be visually checked for completeness before production use.",
            *raw_geometry_warnings,
            *cartography_warnings,
        ],
    }
    metadata.text = json.dumps(
        {
            "generated_at": generated_at,
            "preset": layout.preset,
            "detail_profile": detail_profile,
            "extent_wgs84": layout.bbox.as_dict(),
            "attribution": manifest["source"]["attribution"],
            **(
                {
                    "theme_id": design_contract["theme_id"],
                    "theme_sha256": design_contract["theme_sha256"],
                    "edition_signature_sha256": design_contract[
                        "edition_signature_sha256"
                    ],
                }
                if design_contract is not None
                else {}
            ),
        },
        separators=(",", ":"),
    )

    ET.indent(root, space="  ")
    temporary = output_path.with_suffix(output_path.suffix + f".{os.getpid()}.tmp")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
        os.replace(temporary, output_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MapPlotterError(f"Could not write SVG {output_path}: {exc}") from exc
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MapPlotterError(f"Could not write plot manifest {path}: {exc}") from exc


def write_pen_svgs(
    master_path: Path,
    manifest: dict[str, Any],
    *,
    output_dir: Path | None = None,
    protected_paths: tuple[Path, ...] = (),
) -> list[dict[str, Any]]:
    """Write one page-sized SVG per physical pen step from the master file."""

    try:
        master_root = ET.parse(master_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise MapPlotterError(
            f"Could not read master SVG {master_path}: {exc}"
        ) from exc
    destination = output_dir or master_path.parent
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MapPlotterError(
            f"Could not create per-pen output directory {destination}: {exc}"
        ) from exc

    records: list[dict[str, Any]] = []
    for pen in manifest.get("pen_sequence", []):
        step = int(pen["step"])
        allowed_ids = {f"layer-{layer_id}" for layer_id in pen["layers"]}
        root = deepcopy(master_root)
        for child in list(root):
            if (
                child.tag == _svg("g")
                and child.get("id", "").startswith("layer-")
                and child.get("id") not in allowed_ids
            ):
                root.remove(child)
        root.set(f"{{{MAP_NS}}}pen-step", str(step))
        root.set(f"{{{MAP_NS}}}physical-pen", str(pen["pen"]))
        root.set(f"{{{MAP_NS}}}physical-pen-id", str(pen.get("pen_id", "")))
        root.set(f"{{{MAP_NS}}}pen-profile", str(pen.get("pen_profile", "style")))
        root.set(
            f"{{{MAP_NS}}}pen-slot-status",
            str(pen.get("slot_status", "active")),
        )
        root.set(f"{{{MAP_NS}}}path-count", str(int(pen.get("path_count", 0))))
        root.set(
            f"{{{MAP_NS}}}calibration-state",
            str(pen.get("calibration_state", "nominal-unmeasured")),
        )
        slug = re.sub(
            r"[^a-z0-9]+",
            "-",
            str(pen.get("pen_id") or f"{pen['ink']}-{pen['nib_mm']}").casefold(),
        ).strip("-")
        path = destination / f"{master_path.stem}.pen-{step:02d}-{slug}.svg"
        for protected in protected_paths:
            try:
                same_path = os.path.samefile(path, protected)
            except (FileNotFoundError, OSError):
                same_path = path.resolve() == protected.resolve()
            if same_path:
                raise MapPlotterError(
                    f"Per-pen output path {path} would overwrite input file {protected}."
                )
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        try:
            ET.indent(root, space="  ")
            ET.ElementTree(root).write(
                temporary, encoding="utf-8", xml_declaration=True
            )
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise MapPlotterError(f"Could not write per-pen SVG {path}: {exc}") from exc
        records.append(
            {
                "step": step,
                "pen": pen["pen"],
                "pen_id": pen.get("pen_id"),
                "pen_profile": pen.get("pen_profile", "style"),
                "ink": pen["ink"],
                "nib_mm": pen["nib_mm"],
                "nominal_nib_mm": pen.get("nominal_nib_mm", pen["nib_mm"]),
                "calibration_state": pen.get("calibration_state", "nominal-unmeasured"),
                "calibration_substrate": pen.get("calibration_substrate"),
                "configured_layers": list(pen.get("configured_layers", pen["layers"])),
                "layers": list(pen["layers"]),
                "omitted_layers": list(pen.get("omitted_layers", [])),
                "path_count": int(pen.get("path_count", 0)),
                "empty": bool(pen.get("empty", False)),
                "slot_status": str(pen.get("slot_status", "active")),
                "path": str(path.resolve()),
            }
        )
    return records
