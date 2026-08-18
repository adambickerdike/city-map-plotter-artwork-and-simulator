from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable
from xml.etree import ElementTree as ET

import pytest

from city_map_plotter.f1_circuits import build_f1_plate, load_f1_catalog
from city_map_plotter.niche_common import write_plate


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import qa_f1_circuit_series as qa  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "f1-synthetic-complete-v1.json"
CANONICAL_CATALOG = ROOT / "src/city_map_plotter/data/f1-circuits-2026.json"
LEGACY_CATALOG = ROOT / "src/city_map_plotter/data/f1-circuits-legacy-v1.json"
SVG_NS = qa.SVG_NS
SVG = f"{{{SVG_NS}}}"


def _synthetic_official_facts() -> dict[str, Any]:
    return {
        "official_circuit_length_m": 400.0,
        "first_grand_prix": 2000,
        "fastest_lap": {
            "status": "source-backed",
            "time": "1:02.345",
            "time_ms": 62345,
            "driver": "Ada Lovelace",
            "season": 2025,
            "source_ref": "synthetic-circuit-source",
        },
        "source_ref": "synthetic-circuit-source",
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _path(group: ET.Element, data: str, role: str, **attributes: str) -> ET.Element:
    return ET.SubElement(
        group,
        f"{SVG}path",
        {"d": data, "data-role": role, **attributes},
    )


def _group(
    root: ET.Element, step: int, pen_id: str, ink: str, nib: float
) -> ET.Element:
    return ET.SubElement(
        root,
        f"{SVG}g",
        {
            "id": f"layer-pen-{pen_id}",
            "data-pen-step": str(step),
            "data-plot-pen-id": pen_id,
            "data-plot-ink": ink,
            "data-plot-nib-mm": f"{nib:g}",
            "fill": "none",
            "stroke": {
                "Grey": "#94a3b8",
                "Green": "#86b99b",
                "Blue": "#6baed6",
                "Purple": "#a78bba",
                "Red": "#ef4444",
                "Black": "#18181b",
            }[ink],
            "stroke-width": f"{nib:g}",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )


def _synthetic_svg(artifact_id: str, geometry_hash: str) -> ET.Element:
    fixture_catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture_sources = fixture_catalog["sources"]
    model = fixture_catalog["events"][0]["circuit"]["geometry"]["model"]
    lap_hash = qa.canonical_lap_sha256(model["lap"])
    coordinate_count = len(qa._geojson_coordinates(model["lap"]))
    root = ET.Element(
        f"{SVG}svg",
        {
            "width": "148mm",
            "height": "210mm",
            "viewBox": "0 0 148 210",
            "version": "1.1",
            "data-source-geometry-sha256": geometry_hash,
        },
    )
    metadata = ET.SubElement(root, f"{SVG}metadata")
    metadata.text = json.dumps(
        {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "subject_id": "synthetic-grand-prix",
            "format_id": "a5-portrait",
            "source_geometry_sha256": geometry_hash,
            "sources": fixture_sources,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    grey = _group(root, 1, "grey-0-25", "Grey", 0.25)
    _path(grey, "M 12 35.122 L 136 35.122 L 136 164.046 L 12 164.046 Z", "field-frame")
    _path(
        grey,
        "M 32 57 L 118 57 L 118 143 L 32 143 Z",
        "track-boundary",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-boundary-object",
            "data-boundary-id": "synthetic-boundary-outer",
            "data-boundary-index": "0",
            "data-lap-associated": "true",
            "data-boundary-geometry-kind": "source-area",
            "data-association-policy": "source-area-covers-selected-lap-v1",
            "data-lap-coverage-fraction": "1",
        },
    )
    _path(
        grey,
        "M 20 150 L 130 150",
        "host-road",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-road-object",
        },
    )

    green = _group(root, 2, "green-0-25", "Green", 0.25)
    _path(
        green,
        "M 20 155 L 26 158 L 32 155",
        "vegetation",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-road-object",
        },
    )

    blue = _group(root, 3, "blue-0-25", "Blue", 0.25)
    _path(
        blue,
        "M 126 48 L 126 82",
        "water",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-road-object",
        },
    )

    purple = _group(root, 4, "purple-0-4", "Purple", 0.4)
    _path(
        purple,
        "M 35 60 L 50 66 L 100 66 L 115 60",
        "pit-lane",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-pit-object",
            "data-pit-id": "synthetic-pit-main",
            "data-entry-station-fraction": "0",
            "data-exit-station-fraction": "0.25",
        },
    )
    _path(
        purple,
        "M 42 64 L 70 64",
        "operational-overlay",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-tunnel-object",
            "data-section-id": "synthetic-tunnel-section",
            "data-operational-kind": "tunnel",
        },
    )

    red = _group(root, 5, "red-0-4", "Red", 0.4)
    _path(
        red,
        "M 35 140 L 115 140 L 115 60 L 35 60 Z",
        "lap-centreline",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-lap-object",
            "data-source-geometry-sha256": geometry_hash,
            "data-source-lap-sha256": lap_hash,
            "data-source-coordinate-count": str(coordinate_count),
            "data-projected-coordinate-count": str(coordinate_count),
            "data-centreline-parity": "exact-projected-source-coordinate-order",
            "data-racing-line": "false",
        },
    )
    for group_id, side, radius, data in (
        (
            "radius-1-outer",
            "outer",
            "0.2",
            "M 34.8 59.8 L 115.2 59.8 L 115.2 140.2 L 34.8 140.2 Z",
        ),
        (
            "radius-1-inner",
            "inner",
            "0.2",
            "M 35.2 60.2 L 114.8 60.2 L 114.8 139.8 L 35.2 139.8 Z",
        ),
    ):
        _path(
            red,
            data,
            "diagrammatic-course-corridor-offset",
            **{
                "data-source-ref": "synthetic-circuit-source",
                "data-source-object-id": "synthetic-lap-object",
                "data-source-geometry-sha256": geometry_hash,
                "data-source-lap-sha256": lap_hash,
                "data-claim": "DIAGRAMMATIC COURSE CORRIDOR",
                "data-diagrammatic": "true",
                "data-racing-line": "false",
                "data-surveyed-track-width": "false",
                "data-offset-fallback": "false",
                "data-offset-group-id": group_id,
                "data-offset-side": side,
                "data-offset-radius-mm": radius,
                "data-offset-part-index": "0",
                "data-course-target-width-mm": "0.8",
                "data-clearance-clipped": "false",
                "data-clearance-zone-ids": "",
                "data-derivation": (
                    "buffer-envelope-from-exact-sourced-lap-centreline"
                ),
            },
        )

    black = _group(root, 6, "black-0-25", "Black", 0.25)
    _path(
        black,
        "M 35 57 L 35 63",
        "start-finish",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-lap-object",
            "data-station-fraction": "0",
        },
    )
    markers = (
        ("turn-1", 75.0, 60.0, "M 71 49 L 73 51", "70,48,4,4"),
        ("turn-2", 115.0, 100.0, "M 123 97 L 125 99", "122,96,4,4"),
        ("turn-3", 75.0, 140.0, "M 71 151 L 73 153", "70,150,4,4"),
        ("turn-4", 35.0, 100.0, "M 25 97 L 27 99", "24,96,4,4"),
    )
    for turn_id, x, y, label_data, label_box in markers:
        _path(
            black,
            f"M {x - 1:g} {y:g} L {x:g} {y - 1:g} L {x + 1:g} {y:g} L {x:g} {y + 1:g} Z",
            "turn-marker",
            **{
                "data-turn-id": turn_id,
                "data-derivation": "station-on-lap",
            },
        )
        _path(
            black,
            label_data,
            "turn-label",
            **{
                "data-turn-id": turn_id,
                "data-label-id": f"label-{turn_id}",
                "data-label-box": label_box,
                "data-derivation": "station-on-lap",
            },
        )
    _path(
        black,
        "M 20 196 L 42 196",
        "attribution",
        **{
            "data-label-id": "attribution",
            "data-label-box": "18,194.2,26,3",
        },
    )

    heavy = _group(root, 7, "black-1", "Black", 1.0)
    _path(heavy, "M 6 6 L 142 6 L 142 204 L 6 204 Z", "outer-border")
    _path(
        heavy,
        "M 55 18 L 93 18",
        "title",
        **{
            "data-label-id": "title",
            "data-label-box": "52,14,44,8",
            "data-title-block-id": "plate-title",
            "data-title-line-index": "0",
            "data-title-line-count": "1",
        },
    )
    return root


def _motion(root: ET.Element) -> tuple[float, float]:
    failures: list[str] = []
    paths = qa._paths_with_evidence(root, failures)
    assert not failures
    down = sum(path.length for path in paths)
    up = 0.0
    current = (6.0, 6.0)
    for group in qa._physical_groups(root):
        for path in [item for item in paths if item.group is group]:
            for subpath in path.subpaths:
                if current is not None:
                    up += (
                        (subpath.start[0] - current[0]) ** 2
                        + (subpath.start[1] - current[1]) ** 2
                    ) ** 0.5
                current = subpath.end
    return down, up


def _write_split_files(
    series: Path, artifact_id: str, root: ET.Element
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for group in qa._physical_groups(root):
        pen_id = str(group.get("data-plot-pen-id"))
        step = int(str(group.get("data-pen-step")))
        split_root = copy.deepcopy(root)
        for child in list(split_root):
            if child.tag == f"{SVG}g" and child.get("data-pen-step") is not None:
                if child.get("data-plot-pen-id") != pen_id:
                    split_root.remove(child)
        path = series / f"{artifact_id}.pen-{step:02d}-{pen_id}.svg"
        ET.ElementTree(split_root).write(path, encoding="utf-8", xml_declaration=True)
        records.append(
            {
                "step": step,
                "pen_id": pen_id,
                "path": path.name,
                "sha256": _sha256(path),
            }
        )
    return records


def _build_release(tmp_path: Path) -> tuple[Path, Path]:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    catalog["events"][0]["official_facts"] = _synthetic_official_facts()
    catalog_path = tmp_path / "catalog.json"
    _write_json(catalog_path, catalog)
    catalog_hash = _sha256(catalog_path)
    event = catalog["events"][0]
    model = event["circuit"]["geometry"]["model"]
    geometry_hash = model["geometry_sha256"]
    lap_hash = qa.canonical_lap_sha256(model["lap"])
    lap_coordinate_count = len(qa._geojson_coordinates(model["lap"]))
    series = tmp_path / "release"
    series.mkdir()
    artifact_id = "synthetic-grand-prix--a5-portrait"
    svg_path = series / f"{artifact_id}.svg"
    root = _synthetic_svg(artifact_id, geometry_hash)
    ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)
    pen_files = _write_split_files(series, artifact_id, root)
    down, up = _motion(root)
    zones = {
        "title": {"x": 12.0, "y": 12.0, "width": 124.0, "height": 12.6},
        "subtitle": {"x": 12.0, "y": 27.6, "width": 124.0, "height": 4.522},
        "map_field": {"x": 12.0, "y": 35.122, "width": 124.0, "height": 128.924},
        "furniture": {"x": 12.0, "y": 167.046, "width": 124.0, "height": 5.426},
        "detail": {"x": 12.0, "y": 175.473, "width": 124.0, "height": 15.523},
        "attribution": {"x": 12.0, "y": 193.996, "width": 124.0, "height": 4.004},
    }
    map_zone = qa._rect(zones["map_field"])
    assert map_zone is not None
    measured_paths = qa._paths_with_evidence(root, [])
    field_baseline = sum(
        path.length for path in measured_paths if qa._inside(path.bounds, map_zone)
    )
    field_area = zones["map_field"]["width"] * zones["map_field"]["height"]
    target_length = field_area * qa.F1_DESIGN_DENSITY_MM_PER_MM2
    hard_length = field_area * qa.MAX_DENSITY_MM_PER_MM2
    pen_sequence = [
        {
            "step": int(str(group.get("data-pen-step"))),
            "pen_id": group.get("data-plot-pen-id"),
        }
        for group in qa._physical_groups(root)
    ]
    details = [
        "LENGTH 0.400 KM / FIRST GP 2000",
        "FASTEST LAP 1:02.345 / ADA LOVELACE / 2025",
        "TURNS 1-4 GEOMETRIC / NO WIDTH OR RACING LINE",
    ]
    information_groups = [
        {
            "id": "course",
            "label": "COURSE",
            "lines": ["0.400 KM", "CLOCKWISE"],
        },
        {
            "id": "history",
            "label": "FORMULA 1",
            "lines": ["FIRST GRAND PRIX 2000", "SEASON 2026 REFERENCE"],
        },
        {
            "id": "record",
            "label": "FASTEST LAP",
            "lines": ["1:02.345 / 2025", "ADA LOVELACE"],
        },
        {
            "id": "drawing",
            "label": "DIAGRAMMATIC COURSE",
            "lines": ["SOURCE CENTRELINE", "G01-G04 / GEOMETRIC"],
        },
    ]
    visible_information_lines = [
        copy_value
        for group in information_groups
        for copy_value in [group["label"], *group["lines"]]
    ]
    manifest_path = series / f"{artifact_id}.plot.json"
    manifest = {
        "schema_version": 2,
        "artifact_kind": qa.ARTIFACT_KIND,
        "artifact_id": artifact_id,
        "subject_id": event["id"],
        "variant_id": "a5-portrait",
        "format_id": "a5-portrait",
        "data_snapshot": qa.FREEZE_DATE,
        "domain": "f1-circuits",
        "details": details,
        "sources": catalog["sources"],
        "page": {
            "paper": "A5",
            "orientation": "portrait",
            "width_mm": 148.0,
            "height_mm": 210.0,
            "margin_mm": 6.0,
            "format_id": "a5-portrait",
            "zones_mm": zones,
            "title_line_layout": {
                "maximum_lines": 2,
                "nib_mm": 1.0,
                "horizontal_ink_inset_mm": 0.5,
                "min_ink_clearance_nib_multiple": 1.0,
                "min_ink_clearance_mm": 1.0,
                "min_path_bounds_gap_mm": 2.0,
            },
        },
        "rendering": {
            "f1_circuit": {
                "source_geometry_sha256": geometry_hash,
                "catalog_sha256": catalog_hash,
                "field_pen_order": list(qa.F1_FIELD_PEN_ORDER),
                "course_facts": {
                    "policy": "source-backed-information-rail-v2",
                    "length": {
                        "status": "source-backed",
                        "value_m": 400.0,
                        "display_copy": "0.400 KM",
                        "source_ref": "synthetic-circuit-source",
                    },
                    "first_grand_prix": {
                        "status": "source-backed",
                        "year": 2000,
                        "source_ref": "synthetic-circuit-source",
                        "scope": ("formula1-official-page-first-grand-prix-venue-fact"),
                    },
                    "fastest_lap": copy.deepcopy(
                        event["official_facts"]["fastest_lap"]
                    ),
                    "configuration_reference_season": 2026,
                    "summary_lines": details,
                    "visible_groups": information_groups,
                    "visible_lines": visible_information_lines,
                    "diagrammatic_course_disclosure_visible": True,
                    "full_driver_copy_preserved": True,
                },
                "diagrammatic_course_corridor": {
                    "policy": "paired-buffer-envelope-passes-from-exact-centreline-v1",
                    "local_clearance_policy": ("derived-corridor-local-clearance-v1"),
                    "minimum_safe_edge_gap_mm": 1.2,
                    "clearance_zone_count": 0,
                    "clearance_zones": [],
                    "red_centreline_modified_for_local_clearance": False,
                    "white_ink_used_for_local_clearance": False,
                    "source_centreline_path_count": 1,
                    "source_centreline_coordinate_count": lap_coordinate_count,
                    "source_lap_sha256": lap_hash,
                    "target_width_mm": 0.8,
                    "pen_id": "red-0-4",
                    "nib_mm": 0.4,
                    "pair_pitch_mm": 0.2,
                    "radii_mm": [0.2],
                    "logical_stroke_count": 3,
                    "plotted_width_mm": 0.8,
                    "expected_offset_group_count": 2,
                    "emitted_offset_group_count": 2,
                    "emitted_offset_path_count": 2,
                    "offset_groups": [
                        {
                            "id": "radius-1-outer",
                            "side": "outer",
                            "radius_mm": 0.2,
                            "path_count": 1,
                            "closed_path_count": 1,
                            "open_path_count": 0,
                            "clearance_clipped_path_count": 0,
                            "length_mm": 321.6,
                        },
                        {
                            "id": "radius-1-inner",
                            "side": "inner",
                            "radius_mm": 0.2,
                            "path_count": 1,
                            "closed_path_count": 1,
                            "open_path_count": 0,
                            "clearance_clipped_path_count": 0,
                            "length_mm": 318.4,
                        },
                    ],
                    "hold_on_offset_failure": True,
                    "offset_fallback_allowed": False,
                    "surveyed_track_width_claimed": False,
                    "racing_line_claimed": False,
                },
                "context_features": {
                    "mode": "permanent",
                    "mode_source": "event.circuit.atlas_context_mode",
                    "mode_derived_from_site_type": False,
                    "mode_override_applied": False,
                    "vegetation_outline_policy": (
                        "outline-only-density-budgeted-source-boundary"
                    ),
                    "vegetation_outline_budget": {
                        "policy": "outline-only-density-budgeted-source-boundary",
                        "target_field_density_mm_per_mm2": (
                            qa.F1_DESIGN_DENSITY_MM_PER_MM2
                        ),
                        "configured_reserve_density_mm_per_mm2": (
                            qa.F1_VEGETATION_RESERVE_MM_PER_MM2
                        ),
                        "requested_outline_reserve_mm": 0.0,
                        "field_area_mm2": round(field_area, 6),
                        "baseline_pen_down_mm_excluding_vegetation_outlines": round(
                            field_baseline, 6
                        ),
                        "available_vegetation_outline_mm": round(
                            max(0.0, target_length - field_baseline), 6
                        ),
                        "candidate_vegetation_outline_mm": 0.0,
                        "retained_vegetation_outline_mm": 0.0,
                        "omitted_vegetation_outline_mm": 0.0,
                        "mandatory_zero_symbol_outline_mm": 0.0,
                        "mandatory_outline_overage_mm": 0.0,
                        "candidate_feature_ids": [],
                        "retained_feature_ids": [],
                        "omitted_feature_ids": [],
                        "candidate_feature_count": 0,
                        "retained_feature_count": 0,
                        "omitted_feature_count": 0,
                        "mandatory_zero_symbol_feature_ids": [],
                        "mandatory_zero_symbol_feature_count": 0,
                        "candidate_features": [],
                        "whole_feature_groups_only": True,
                        "interior_symbols_retained_independently": False,
                        "vegetation_interior_pattern": "none-outline-only",
                        "retention_rank_recipe": [
                            "mandatory-zero-symbol-first",
                            "named-before-unnamed",
                            "woodland-before-grass",
                            "stable-feature-id",
                            "whole-group-must-fit",
                        ],
                        "projected_field_density_mm_per_mm2": round(
                            field_baseline / field_area, 9
                        ),
                    },
                    "input_counts_by_kind": {"grandstand": 0},
                    "selected_counts_by_kind": {"grandstand": 0},
                    "emitted_path_counts_by_kind": {"grandstand": 0},
                    "water_stipple_dot_count": 0,
                    "vegetation_symbol_count": 0,
                    "context_density_budget": {
                        "policy": (
                            "decoration-then-whole-unlabelled-source-feature-"
                            "hard-water-boundary-fallback-v2"
                        ),
                        "initial_baseline_length_mm": round(field_baseline, 6),
                        "maximum_baseline_length_mm": round(target_length, 6),
                        "hard_maximum_baseline_length_mm": round(hard_length, 6),
                        "retained_baseline_length_mm": round(field_baseline, 6),
                        "target_overage_mm": round(
                            max(0.0, field_baseline - target_length), 6
                        ),
                        "removed_length_mm": 0.0,
                        "decisions": [],
                        "labelled_context_features_protected": True,
                        "course_and_station_geometry_protected": True,
                        "whole_source_feature_groups_only_after_decoration": True,
                    },
                    "track_boundary_qualifications": [
                        {
                            "boundary_index": 0,
                            "geometry_kind": "source-area",
                            "association_policy": (
                                "source-area-covers-selected-lap-v1"
                            ),
                            "lap_coverage_fraction": 1.0,
                            "representative_clearance_mm": 3.0,
                            "required_clearance_mm": 0.75,
                            "lap_associated": True,
                            "resolvable": True,
                            "reason": None,
                        }
                    ],
                    "track_boundary_emitted_feature_count": 1,
                    "track_boundary_emitted_path_count": 1,
                    "grandstand_observation": {
                        "policy": "frozen-current-osm-footprint-only-v1",
                        "source_record_count": 0,
                        "selected_feature_count": 0,
                        "emitted_feature_count": 0,
                        "emitted_path_count": 0,
                        "source_feature_ids": [],
                        "selected_feature_ids": [],
                        "emitted_feature_ids": [],
                        "source_unselected_feature_ids": [],
                        "culled_feature_ids": [],
                        "partition_policy": (
                            "source=selected+unselected; selected=emitted+culled; "
                            "every non-emitted id requires feature_omissions evidence"
                        ),
                        "claim_scope": (
                            "current-osm-grandstand-footprint-only-not-event-or-fia-configuration"
                        ),
                        "event_configuration_verified": False,
                        "fia_configuration_claimed": False,
                        "operational_semantics_claimed": False,
                        "visible_disclosure": None,
                    },
                },
                "topology": {
                    "self_crossing_or_grade_separation_review_required": False,
                    "lap_self_intersection_count": 0,
                    "lap_self_intersection_segment_indexes": [],
                    "grade_separation_source_section_ids": [],
                    "grade_separation_cue_required": False,
                    "grade_separation_cue_policy": (
                        "black-bridge-deck-bracket-after-red-v2"
                    ),
                    "grade_separation_cue_emitted_path_count": 0,
                    "red_centreline_modified_for_grade_separation_cue": False,
                    "white_ink_used_for_grade_separation_cue": False,
                },
                "paper_adaptation": {
                    "format_id": "a5-portrait",
                    "context_mode": "permanent",
                    "context_mode_source": "event.circuit.atlas_context_mode",
                    "context_mode_derived_from_site_type": False,
                    "context_mode_override_applied": False,
                    "framing_source_scope": (
                        "lap-plus-pit-only-unqualified-boundaries-excluded-v2"
                    ),
                    "framing_fit_policy": (
                        "maximum-safe-contain-no-geographic-margin-v1"
                    ),
                    "structural_source_bounds_m": [0.0, 0.0, 100.0, 100.0],
                    "unqualified_raw_boundary_count_excluded_from_framing": 1,
                    "source_bounds_m": [0.0, 0.0, 100.0, 100.0],
                    "context_viewport_source_bounds_m": [-5.0, -5.0, 105.0, 105.0],
                    "course_edge_clearance_mm": 4.0,
                    "working_rect_mm": {
                        "x": 35.0,
                        "y": 60.0,
                        "width": 80.0,
                        "height": 80.0,
                    },
                    "scale_mm_per_m": 0.8,
                    "approximate_scale_denominator": 1250,
                    "structural_bounds_mm": [35.0, 60.0, 115.0, 140.0],
                    "structural_width_utilization": 1.0,
                    "structural_height_utilization": 1.0,
                    "maximum_safe_axis_utilization": 1.0,
                    "hero_bounds_mm": [35.0, 60.0, 115.0, 140.0],
                    "hero_width_utilization": 1.0,
                    "hero_height_utilization": 1.0,
                    "sheet_gate": {"field_padding_mm": 4.0},
                },
                "feature_omissions": [],
                "track_clearance": {
                    "policy": "source-space-subtraction",
                    "minimum_clearance_mm": 0.75,
                    "host_road_halo_emitted_as_path": False,
                },
            }
        },
        "pen_sequence": pen_sequence,
        "plot_summary": {
            "pen_down_distance_mm": down,
            "pen_up_travel_mm": up,
            "travel_ratio": up / down,
            "estimated_plot_seconds_including_pen_up": down / 35.0 + up / 80.0,
        },
        "outputs": {
            "svg": {"path": svg_path.name, "sha256": _sha256(svg_path)},
            "manifest": {"path": manifest_path.name},
            "pen_files": pen_files,
        },
        "catalog_record": event,
    }
    _write_json(manifest_path, manifest)
    entry = {
        "id": artifact_id,
        "artifact_kind": qa.ARTIFACT_KIND,
        "event_id": event["id"],
        "format_id": "a5-portrait",
        "calendar_status": "confirmed",
        "catalog_sha256": catalog_hash,
        "source_geometry_sha256": geometry_hash,
        "outputs": {
            "svg": {"path": svg_path.name, "sha256": _sha256(svg_path)},
            "manifest": {"path": manifest_path.name, "sha256": _sha256(manifest_path)},
        },
    }
    index = {
        "schema_version": 1,
        "artifact_kind": qa.ARTIFACT_KIND,
        "catalog_id": catalog["catalog_id"],
        "catalog_sha256": catalog_hash,
        "formats": ["a5-portrait"],
        "entries": [entry],
    }
    _write_json(series / "index.json", index)
    return series, catalog_path


def _audit(
    series: Path,
    catalog: Path,
    *,
    expected_event_count: int = 1,
    event_ids: list[str] | None = None,
    complete_release: bool = False,
) -> dict[str, Any]:
    return qa.audit_f1_circuit_series(
        series,
        catalog_file=catalog,
        expected_event_count=expected_event_count,
        event_ids=event_ids,
        complete_release=complete_release,
    )


def _entry(series: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    index = json.loads((series / "index.json").read_text(encoding="utf-8"))
    return index, index["entries"][0]


def _master(series: Path) -> tuple[Path, ET.Element]:
    _index, entry = _entry(series)
    path = series / entry["outputs"]["svg"]["path"]
    return path, ET.parse(path).getroot()


def _sync_master(series: Path, root: ET.Element) -> None:
    index, entry = _entry(series)
    svg_path = series / entry["outputs"]["svg"]["path"]
    ET.ElementTree(root).write(svg_path, encoding="utf-8", xml_declaration=True)
    entry["outputs"]["svg"]["sha256"] = _sha256(svg_path)
    manifest_path = series / entry["outputs"]["manifest"]["path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"]["svg"]["sha256"] = _sha256(svg_path)
    manifest["outputs"]["pen_files"] = _write_split_files(series, entry["id"], root)
    down, up = _motion(root)
    manifest["plot_summary"].update(
        {
            "pen_down_distance_mm": down,
            "pen_up_travel_mm": up,
            "travel_ratio": up / down,
            "estimated_plot_seconds_including_pen_up": down / 35.0 + up / 80.0,
        }
    )
    _write_json(manifest_path, manifest)
    entry["outputs"]["manifest"]["sha256"] = _sha256(manifest_path)
    _write_json(series / "index.json", index)


def _sync_manifest(series: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    index, entry = _entry(series)
    manifest_path = series / entry["outputs"]["manifest"]["path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    _write_json(manifest_path, manifest)
    entry["outputs"]["manifest"]["sha256"] = _sha256(manifest_path)
    _write_json(series / "index.json", index)


def _rebind_catalog(
    series: Path,
    catalog_path: Path,
    catalog: dict[str, Any],
    *,
    root: ET.Element | None = None,
) -> None:
    event = catalog["events"][0]
    model = event["circuit"]["geometry"]["model"]
    model["geometry_sha256"] = qa.canonical_geometry_sha256(model)
    geometry_hash = model["geometry_sha256"]
    lap_hash = qa.canonical_lap_sha256(model["lap"])
    _write_json(catalog_path, catalog)
    catalog_hash = _sha256(catalog_path)

    if root is None:
        _path_value, root = _master(series)
    root.set("data-source-geometry-sha256", geometry_hash)
    for path in root.iter(f"{SVG}path"):
        if path.get("data-source-geometry-sha256") is not None:
            path.set("data-source-geometry-sha256", geometry_hash)
        if path.get("data-source-lap-sha256") is not None:
            path.set("data-source-lap-sha256", lap_hash)
    metadata = next(root.iter(f"{SVG}metadata"))
    payload = json.loads(metadata.text or "{}")
    payload["source_geometry_sha256"] = geometry_hash
    metadata.text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    _sync_master(series, root)

    index, entry = _entry(series)
    index["catalog_sha256"] = catalog_hash
    entry["catalog_sha256"] = catalog_hash
    entry["source_geometry_sha256"] = geometry_hash
    manifest_path = series / entry["outputs"]["manifest"]["path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    f1 = manifest["rendering"]["f1_circuit"]
    f1["source_geometry_sha256"] = geometry_hash
    f1["catalog_sha256"] = catalog_hash
    corridor = f1.get("diagrammatic_course_corridor", {})
    corridor["source_lap_sha256"] = lap_hash
    manifest["catalog_record"] = event
    _write_json(manifest_path, manifest)
    entry["outputs"]["manifest"]["sha256"] = _sha256(manifest_path)
    _write_json(series / "index.json", index)


def _all_failures(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            *report["failures"],
            *(
                failure
                for result in report["results"]
                for failure in result["failures"]
            ),
        ]
    )


def test_synthetic_index_scoped_pilot_passes_and_writes_reports(
    tmp_path: Path,
) -> None:
    series, catalog = _build_release(tmp_path)
    report = _audit(series, catalog)
    assert report["passed"] is True, _all_failures(report)
    assert report["technical_pass"] is True
    assert report["scope_mode"] == "index-subset"
    assert report["review_hold"] is True
    assert report["rights_hold"] is True
    assert report["physical_proof_hold"] is True
    assert report["commercial_release_authorized"] is False
    result = report["results"][0]
    assert result["metrics"]["connected_hero_fraction"] == 1.0
    assert result["metrics"]["label_bbox_overlap_count"] == 0
    assert result["metrics"]["label_route_overlap_count"] == 0
    assert result["metrics"]["coverage"] <= qa.MAX_COVERAGE
    assert result["metrics"]["density_mm_per_mm2"] <= qa.MAX_DENSITY_MM_PER_MM2
    json_path, markdown_path = qa.write_qa_artifacts(series, report)
    assert json.loads(json_path.read_text(encoding="utf-8"))["passed"] is True
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Technical result: **PASS**" in markdown
    assert "rights=YES" in markdown


def test_catalog_withheld_fastest_lap_uses_neutral_edition_card(
    tmp_path: Path,
) -> None:
    series, catalog_path = _build_release(tmp_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    fastest = {
        "status": "withheld",
        "withheld_reason": "official-source-placeholder-no-fastest-lap-yet",
        "source_ref": "synthetic-circuit-source",
    }
    catalog["events"][0]["official_facts"]["fastest_lap"] = fastest
    _rebind_catalog(series, catalog_path, catalog)

    def mutate(manifest: dict[str, Any]) -> None:
        manifest["details"][1] = "CURRENT COURSE STUDY / F1 REFERENCE 2026"
        facts = manifest["rendering"]["f1_circuit"]["course_facts"]
        facts["fastest_lap"] = copy.deepcopy(fastest)
        facts["summary_lines"] = list(manifest["details"])
        facts["visible_groups"][2] = {
            "id": "edition",
            "label": "EDITION",
            "lines": ["CIRCUIT ATLAS", "F1 REFERENCE 2026"],
        }
        facts["visible_lines"] = [
            copy_value
            for group in facts["visible_groups"]
            for copy_value in [group["label"], *group["lines"]]
        ]

    _sync_manifest(series, mutate)
    report = _audit(series, catalog_path)
    assert report["technical_pass"] is True, _all_failures(report)


def test_named_course_section_copy_is_source_bound_and_not_promoted() -> None:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    event = catalog["events"][0]
    model = event["circuit"]["geometry"]["model"]
    section = {
        "id": "named-section-boot",
        "kind": "named-course-section",
        "name": "The Boot",
        "name_status": "osm-source-tagged-unverified-not-official",
        "claim_scope": "source-tagged-course-section-name-only",
        "source_ref": "synthetic-circuit-source",
        "source_object_id": "synthetic-section-object",
        "source_objects": ["synthetic-section-object"],
        "geometry": {
            "type": "LineString",
            "coordinates": [[70.0, 100.0], [90.0, 100.0]],
        },
    }
    model["special_sections"].append(section)
    root = _synthetic_svg("synthetic-grand-prix--a5-portrait", model["geometry_sha256"])
    black = next(
        group
        for group in qa._physical_groups(root)
        if group.get("data-plot-pen-id") == "black-0-25"
    )
    label_id = "section-label-named-section-boot"
    label = _path(
        black,
        "M 82 155 L 102 155",
        "section-label",
        **{
            "data-label-id": label_id,
            "data-label-box": "81,153,22,4",
            "data-feature-id": section["id"],
            "data-source-ref": section["source_ref"],
            "data-source-object-id": section["source_object_id"],
            "data-source-feature-ids": section["id"],
            "data-source-name-key": "name",
            "data-source-copy": "The Boot",
            "data-visible-copy": "THE BOOT",
            "data-copy-policy-id": qa.CONTEXT_LABEL_COPY_POLICY_ID,
            "data-normalisation-policy-id": (qa.CONTEXT_LABEL_NORMALISATION_POLICY_ID),
            "data-display-punctuation-policy-id": (
                qa.CONTEXT_LABEL_DISPLAY_PUNCTUATION_POLICY_ID
            ),
            "data-name-status": section["name_status"],
            "data-official-course-name": "false",
            "data-claim-scope": section["claim_scope"],
        },
    )
    failures: list[str] = []
    paths = qa._paths_with_evidence(root, failures)
    assert failures == []
    f1 = {
        "labels": {
            "placements": [
                {
                    "id": label_id,
                    "role": "section-label",
                    "feature_id": section["id"],
                    "source_name_key": "name",
                    "source_copy": "The Boot",
                    "copy": "THE BOOT",
                }
            ],
            "named_course_sections": {
                "emitted_count": 1,
                "official_course_name_claimed": False,
                "name_status": section["name_status"],
                "priority": "before-ordinary-context-copy",
            },
        }
    }
    page = {"paper": "A5"}
    assert qa._named_section_lineage_failures(event, page, f1, paths) == []
    label.set("data-official-course-name", "true")
    paths = qa._paths_with_evidence(root, [])
    assert any(
        "source/name lineage drifted" in failure
        for failure in qa._named_section_lineage_failures(event, page, f1, paths)
    )


def test_serialized_title_paths_with_colliding_ink_envelopes_fail_qa(
    tmp_path: Path,
) -> None:
    series, catalog = _build_release(tmp_path)
    _svg_path, root = _master(series)
    title = next(
        path for path in root.iter(f"{SVG}path") if path.get("data-role") == "title"
    )
    title.set("d", "M 55 17 L 93 17")
    title.set("data-title-line-count", "2")
    title_group = next(
        group for group in qa._physical_groups(root) if title in list(group)
    )
    _path(
        title_group,
        "M 55 18 L 93 18",
        "title",
        **{
            "data-title-block-id": "plate-title",
            "data-title-line-index": "1",
            "data-title-line-count": "2",
        },
    )
    _sync_master(series, root)

    report = _audit(series, catalog)
    failures = _all_failures(report)
    assert report["technical_pass"] is False
    assert "leave only 0.000 mm of white paper; 1.000 mm is required" in failures


def test_serialized_single_line_title_ink_envelope_outside_zone_fails_qa(
    tmp_path: Path,
) -> None:
    series, catalog = _build_release(tmp_path)
    _svg_path, root = _master(series)
    title = next(
        path for path in root.iter(f"{SVG}path") if path.get("data-role") == "title"
    )
    # The centreline remains inside the x=12 mm title-zone edge, while the
    # 1.0 mm round-capped ink envelope extends 0.4 mm beyond it.
    title.set("d", "M 12.1 18 L 50 18")
    _sync_master(series, root)

    report = _audit(series, catalog)
    failures = _all_failures(report)
    assert report["technical_pass"] is False
    assert "title line 0 physical ink envelope leaves the title zone" in failures


def test_serialized_renderer_release_passes_independent_qa(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_bytes(FIXTURE.read_bytes())
    catalog = load_f1_catalog(catalog_path)
    event = catalog["events"][0]
    series = tmp_path / "rendered-release"
    artwork = build_f1_plate(
        event,
        "a5-portrait",
        catalog=catalog,
    )
    outputs = write_plate(
        artwork,
        series,
        png=False,
        split_pens=True,
        generated_at="2026-08-09T00:00:00+00:00",
    )
    catalog_hash = _sha256(catalog_path)
    geometry_hash = event["circuit"]["geometry"]["model"]["geometry_sha256"]
    index = {
        "schema_version": 1,
        "artifact_kind": qa.ARTIFACT_KIND,
        "catalog_id": catalog["catalog_id"],
        "catalog_sha256": catalog_hash,
        "formats": ["a5-portrait"],
        "entries": [
            {
                "id": artwork.artifact_id,
                "artifact_kind": qa.ARTIFACT_KIND,
                "event_id": event["id"],
                "format_id": "a5-portrait",
                "calendar_status": "confirmed",
                "catalog_sha256": catalog_hash,
                "source_geometry_sha256": geometry_hash,
                "outputs": {
                    "svg": outputs["svg"],
                    "manifest": outputs["manifest"],
                },
            }
        ],
    }
    _write_json(series / "index.json", index)
    report = _audit(series, catalog_path)
    assert report["passed"] is True, _all_failures(report)
    result = report["results"][0]
    assert result["metrics"]["lap_closure_mm"] == 0.0
    assert result["metrics"]["connected_hero_fraction"] == 1.0
    assert result["metrics"]["label_bbox_overlap_count"] == 0
    assert result["metrics"]["label_route_overlap_count"] == 0


def test_serialized_centreline_only_release_passes_independent_qa(
    tmp_path: Path,
) -> None:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    event = catalog["events"][0]
    geometry = event["circuit"]["geometry"]
    model = geometry["model"]
    geometry["status"] = qa.CENTRELINE_GEOMETRY_STATUS
    model["qualification"] = {
        "tier": qa.CENTRELINE_GEOMETRY_STATUS,
        "claim_scope": "source-qualified-centreline-cartography-only",
        "omitted_capabilities": [
            "source-backed-start-finish-anchor",
            "source-backed-turn-or-apex-inventory",
            "source-backed-pit-lane",
            "sourced-lap-direction",
        ],
        "omissions_must_be_visibly_disclosed": True,
    }
    model["start_finish"] = None
    model["turn_stations"] = []
    model["pit_lanes"] = []
    event["circuit"]["lap_direction"] = "withheld"
    event["circuit"]["lap_direction_source_ref"] = None
    model["geometry_sha256"] = qa.canonical_geometry_sha256(model)
    catalog_path = tmp_path / "catalog.json"
    _write_json(catalog_path, catalog)
    checked = load_f1_catalog(catalog_path)
    checked_event = checked["events"][0]
    series = tmp_path / "centreline-only-release"
    artwork = build_f1_plate(
        checked_event,
        "a5-portrait",
        catalog=checked,
    )
    outputs = write_plate(
        artwork,
        series,
        png=False,
        split_pens=True,
        generated_at="2026-08-10T00:00:00+00:00",
    )
    catalog_hash = _sha256(catalog_path)
    geometry_hash = checked_event["circuit"]["geometry"]["model"]["geometry_sha256"]
    index = {
        "schema_version": 1,
        "artifact_kind": qa.ARTIFACT_KIND,
        "catalog_id": checked["catalog_id"],
        "catalog_sha256": catalog_hash,
        "formats": ["a5-portrait"],
        "entries": [
            {
                "id": artwork.artifact_id,
                "artifact_kind": qa.ARTIFACT_KIND,
                "event_id": checked_event["id"],
                "format_id": "a5-portrait",
                "calendar_status": "confirmed",
                "catalog_sha256": catalog_hash,
                "source_geometry_sha256": geometry_hash,
                "outputs": {
                    "svg": outputs["svg"],
                    "manifest": outputs["manifest"],
                },
            }
        ],
    }
    _write_json(series / "index.json", index)
    report = _audit(series, catalog_path)
    assert report["technical_pass"] is True, _all_failures(report)
    root = ET.parse(outputs["svg"]["path"]).getroot()
    assert not [
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") in {"start-finish", "turn-marker", "pit-lane"}
    ]


def test_index_scoped_pilot_ignores_unselected_unrenderable_event(
    tmp_path: Path,
) -> None:
    series, catalog_path = _build_release(tmp_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    unavailable = copy.deepcopy(catalog["events"][0])
    unavailable["id"] = "unavailable-grand-prix"
    unavailable["calendar_order"] = 2
    unavailable["circuit"]["id"] = "unavailable-circuit"
    unavailable["circuit"]["geometry"].update({"status": "unavailable", "model": None})
    catalog["events"].append(unavailable)
    _rebind_catalog(series, catalog_path, catalog)

    report = _audit(series, catalog_path, expected_event_count=2)
    assert report["technical_pass"] is True, _all_failures(report)
    assert report["scoped_event_ids"] == ["synthetic-grand-prix"]
    assert report["expected_artifact_count"] == 1

    explicit = _audit(
        series,
        catalog_path,
        expected_event_count=2,
        event_ids=["unavailable-grand-prix"],
    )
    assert explicit["technical_pass"] is False
    failures = _all_failures(explicit)
    assert "circuit.geometry.model is absent" in failures
    assert "explicit-subset matrix is incomplete" in failures


def test_complete_release_scope_requires_all_six_formats(tmp_path: Path) -> None:
    series, catalog = _build_release(tmp_path)
    report = _audit(series, catalog, complete_release=True)
    assert report["technical_pass"] is False
    assert report["scope_mode"] == "complete-release"
    assert report["expected_artifact_count"] == 6
    failures = _all_failures(report)
    assert "requires all six binding formats" in failures
    assert "complete-release matrix is incomplete" in failures


def test_current_event_operational_drs_evidence_is_not_lexically_banned() -> None:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    model = catalog["events"][0]["circuit"]["geometry"]["model"]
    model["operational_overlays"] = {
        "status": "source-backed-current-event",
        "drs_zones": [
            {
                "id": "synthetic-drs-zone",
                "kind": "drs-zone",
                "source_ref": "synthetic-circuit-source",
                "valid_for_season": 2026,
                "document_version": "synthetic-event-map-v1",
                "evidence_scope": "current-event-document",
            }
        ],
    }
    model["geometry_sha256"] = qa.canonical_geometry_sha256(model)
    assert qa.validate_f1_catalog(catalog, expected_event_count=1) == []


def test_operational_drs_claim_without_current_event_evidence_is_held() -> None:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    model = catalog["events"][0]["circuit"]["geometry"]["model"]
    model["operational_overlays"] = {
        "status": "source-backed-current-event",
        "drs_zones": [
            {
                "id": "synthetic-drs-zone",
                "kind": "drs-zone",
                "source_ref": "synthetic-circuit-source",
            }
        ],
    }
    model["geometry_sha256"] = qa.canonical_geometry_sha256(model)
    failures = qa.validate_f1_catalog(catalog, expected_event_count=1)
    assert any("valid_for_season" in failure for failure in failures)
    assert any("document_version" in failure for failure in failures)
    assert any("evidence_scope" in failure for failure in failures)


def test_raw_osm_tags_source_ref_is_not_treated_as_catalog_evidence() -> None:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    event = catalog["events"][0]
    model = event["circuit"]["geometry"]["model"]
    model["context"][0]["tags"].update(
        {
            "source_ref": "https://wiki.openstreetmap.org/wiki/Key:source_ref",
            "nested_raw_tags": {"survey_source_ref": "Baku municipal survey notation"},
        }
    )
    model["geometry_sha256"] = qa.canonical_geometry_sha256(model)

    assert qa.validate_f1_catalog(catalog, expected_event_count=1) == []
    referenced = qa._referenced_source_ids(event)
    assert "https://wiki.openstreetmap.org/wiki/Key:source_ref" not in referenced
    assert "Baku municipal survey notation" not in referenced
    assert referenced == {"synthetic-calendar-source", "synthetic-circuit-source"}


def test_genuine_nested_evidence_source_ref_drift_is_still_held() -> None:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    event = catalog["events"][0]
    model = event["circuit"]["geometry"]["model"]
    model["context"][0]["evidence"] = {
        "survey_source_ref": "unregistered-survey-evidence"
    }
    model["geometry_sha256"] = qa.canonical_geometry_sha256(model)

    failures = qa.validate_f1_catalog(catalog, expected_event_count=1)
    assert any(
        "unresolved source_ref 'unregistered-survey-evidence'" in failure
        for failure in failures
    )
    assert "unregistered-survey-evidence" in qa._referenced_source_ids(event)


def test_canonical_source_qualified_events_pass_semantic_catalog_qa() -> None:
    catalog = json.loads(CANONICAL_CATALOG.read_text(encoding="utf-8"))
    event_ids = [
        str(event["id"])
        for event in catalog["events"]
        if event["circuit"]["geometry"].get("model") is not None
        and event["circuit"]["geometry"].get("status") == "source-qualified"
    ]
    assert event_ids

    failures = qa.validate_f1_catalog(
        catalog,
        expected_event_count=qa.EXPECTED_EVENT_COUNT,
        event_ids=event_ids,
    )
    assert failures == []


def test_legacy_renderable_events_pass_season_and_lineage_qa() -> None:
    catalog = json.loads(LEGACY_CATALOG.read_text(encoding="utf-8"))
    event_ids = [
        str(event["id"])
        for event in catalog["events"]
        if event["circuit"]["geometry"].get("status") == qa.CENTRELINE_GEOMETRY_STATUS
    ]
    assert len(event_ids) == 19

    failures = qa.validate_f1_catalog(
        catalog,
        expected_event_count=len(catalog["events"]),
        event_ids=event_ids,
    )
    assert failures == []


def test_legacy_lap_requires_exact_ordered_multi_object_binding() -> None:
    catalog = json.loads(LEGACY_CATALOG.read_text(encoding="utf-8"))
    event = next(
        event for event in catalog["events"] if event["id"] == "bahrain-grand-prix-2025"
    )
    model = event["circuit"]["geometry"]["model"]
    model["lap"]["properties"]["source_objects"] = list(
        reversed(model["lap"]["properties"]["source_objects"])
    )
    source_ids = {str(source["id"]) for source in catalog["sources"]}

    failures = qa.validate_f1_event(
        event,
        source_ids=source_ids,
        legacy_catalog=True,
    )
    assert any(
        "ordered source_objects do not exactly bind lap_source_objects" in failure
        for failure in failures
    )


def test_legacy_catalog_freeze_is_independently_pinned() -> None:
    catalog = json.loads(LEGACY_CATALOG.read_text(encoding="utf-8"))
    catalog["freeze"]["frozen_at"] = "2026-08-10T00:00:00Z"
    failures = qa.validate_f1_catalog(
        catalog,
        expected_event_count=len(catalog["events"]),
        event_ids=["bahrain-grand-prix-2025"],
    )
    assert "catalog freeze must be 2026-08-11" in failures


def test_svg_drs_claim_with_matching_current_event_evidence_passes(
    tmp_path: Path,
) -> None:
    series, catalog_path = _build_release(tmp_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    model = catalog["events"][0]["circuit"]["geometry"]["model"]
    model["operational_overlays"] = {
        "status": "source-backed-current-event",
        "drs_zones": [
            {
                "id": "synthetic-drs-zone",
                "kind": "drs-zone",
                "source_ref": "synthetic-circuit-source",
                "valid_for_season": 2026,
                "document_version": "synthetic-event-map-v1",
                "evidence_scope": "current-event-document",
            }
        ],
    }
    _path_value, root = _master(series)
    overlay = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "operational-overlay"
    )
    overlay.set("data-operational-kind", "drs-zone")
    overlay.set("data-section-id", "synthetic-drs-zone")
    overlay.set("data-valid-for-season", "2026")
    overlay.set("data-document-version", "synthetic-event-map-v1")
    overlay.set("data-evidence-scope", "current-event-document")
    _rebind_catalog(series, catalog_path, catalog, root=root)

    report = _audit(series, catalog_path)
    assert report["technical_pass"] is True, _all_failures(report)


def test_production_ledger_requires_22_confirmed_plus_conditional_sepang() -> None:
    catalog = json.loads(FIXTURE.read_text(encoding="utf-8"))
    template = catalog["events"][0]
    events = []
    for index in range(1, 24):
        event = copy.deepcopy(template)
        event["id"] = f"synthetic-event-{index:02d}"
        event["calendar_order"] = index
        event["circuit"]["id"] = f"synthetic-circuit-{index:02d}"
        if index == 23:
            event["calendar_status"] = "conditional"
            event["neutral_display_title"] = "Sepang Conditional Circuit"
            event["circuit"]["official_name"] = "Sepang International Circuit"
        events.append(event)
    catalog["events"] = events
    assert qa.validate_f1_catalog(catalog) == []
    catalog["events"].pop()
    failures = qa.validate_f1_catalog(catalog)
    assert any("expected 23" in failure for failure in failures)
    assert any("one conditional" in failure for failure in failures)


def _tamper_open_lap(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    lap = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "lap-centreline"
    )
    lap.set("d", "M 35 60 L 115 60 L 115 140 L 35 140")
    _sync_master(series, root)


def _tamper_duplicate_turn(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    marker = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "turn-marker"
    )
    parent = next(group for group in qa._physical_groups(root) if marker in list(group))
    parent.append(copy.deepcopy(marker))
    _sync_master(series, root)


def _tamper_missing_turn(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    for group in qa._physical_groups(root):
        for path in list(group):
            if (
                path.get("data-role") == "turn-marker"
                and path.get("data-turn-id") == "turn-4"
            ):
                group.remove(path)
                _sync_master(series, root)
                return
    raise AssertionError("turn-4 marker not found")


def _tamper_disconnected_red(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    red = next(
        group
        for group in qa._physical_groups(root)
        if group.get("data-plot-pen-id") == "red-0-4"
    )
    _path(
        red,
        "M 15 155 L 130 155",
        "lap-segment",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-lap-object",
        },
    )
    _sync_master(series, root)


def _tamper_fake_apex(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    marker = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "turn-marker"
    )
    marker.set("data-role", "derived-racing-apex")
    marker.set("data-derivation", "station-on-lap")
    _sync_master(series, root)


def _tamper_drs(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    overlay = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "operational-overlay"
    )
    overlay.set("data-role", "drs-zone")
    _sync_master(series, root)


def _tamper_overlap(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    label = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-label-id") == "label-turn-1"
    )
    label.set("data-label-box", "70,58,10,5")
    _sync_master(series, root)


def _tamper_label_bbox_overlap(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    for path in root.iter(f"{SVG}path"):
        if path.get("data-label-id") == "label-turn-2":
            path.set("data-label-box", "70,48,4,4")
    _sync_master(series, root)


def _tamper_fake_track_width(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    lap = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "lap-centreline"
    )
    lap.set("data-track-width-mm", "12")
    _sync_master(series, root)


def _tamper_source_lineage(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    overlay = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "operational-overlay"
    )
    overlay.attrib.pop("data-source-object-id")
    _sync_master(series, root)


def _tamper_visible_halo(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    grey = next(
        group
        for group in qa._physical_groups(root)
        if group.get("data-plot-pen-id") == "grey-0-25"
    )
    _path(
        grey,
        "M 20 148 L 130 148",
        "host-road-halo",
        **{
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-road-object",
        },
    )
    _sync_master(series, root)


def _tamper_corridor_fallback(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    offset = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-role") == "diagrammatic-course-corridor-offset"
    )
    offset.set("data-offset-fallback", "true")
    _sync_master(series, root)


def _tamper_missing_corridor_group(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    red = next(
        group
        for group in qa._physical_groups(root)
        if group.get("data-plot-pen-id") == "red-0-4"
    )
    offset = next(
        path
        for path in list(red)
        if path.get("data-offset-group-id") == "radius-1-inner"
    )
    red.remove(offset)
    _sync_master(series, root)


def _tamper_parallel_host_road_overdraw(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    host_road = next(
        path for path in root.iter(f"{SVG}path") if path.get("data-role") == "host-road"
    )
    host_road.set("d", "M 40 60.2 L 110 60.2")
    _sync_master(series, root)


def _tamper_short_stroke(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    attribution = next(
        path
        for path in root.iter(f"{SVG}path")
        if path.get("data-label-id") == "attribution"
    )
    attribution.set("d", "M 20 196 L 20.1 196")
    _sync_master(series, root)


def _tamper_canonical_zone(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    water = next(
        path for path in root.iter(f"{SVG}path") if path.get("data-role") == "water"
    )
    water.set("d", "M 126 180 L 126 190")
    _sync_master(series, root)


def _tamper_visible_text(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    text = ET.SubElement(root, f"{SVG}text", {"x": "20", "y": "20"})
    text.text = "RASTER-LIKE COPY"
    _sync_master(series, root)


def _tamper_pen_order(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    groups = qa._physical_groups(root)
    purple = next(
        group for group in groups if group.get("data-plot-pen-id") == "purple-0-4"
    )
    red = next(group for group in groups if group.get("data-plot-pen-id") == "red-0-4")
    children = list(root)
    purple_index = children.index(purple)
    red_index = children.index(red)
    root.remove(purple)
    root.remove(red)
    root.insert(purple_index, red)
    root.insert(red_index, purple)
    red.set("data-pen-step", "4")
    purple.set("data-pen-step", "5")
    _sync_master(series, root)


def _tamper_vegetation_interior_symbol(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    green = next(
        group
        for group in qa._physical_groups(root)
        if group.get("data-plot-pen-id") == "green-0-25"
    )
    _path(
        green,
        "M 42 154 L 44 156 L 46 154",
        "grass-symbol",
        **{
            "data-feature-id": "synthetic-host-road",
            "data-source-ref": "synthetic-circuit-source",
        },
    )
    _sync_master(series, root)


def _tamper_vegetation_wrong_pen(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    groups = qa._physical_groups(root)
    green = next(
        group for group in groups if group.get("data-plot-pen-id") == "green-0-25"
    )
    blue = next(
        group for group in groups if group.get("data-plot-pen-id") == "blue-0-25"
    )
    vegetation = next(
        path for path in list(green) if path.get("data-role") == "vegetation"
    )
    green.remove(vegetation)
    blue.append(vegetation)
    _sync_master(series, root)


def _tamper_water_wrong_pen(series: Path, _catalog: Path) -> None:
    _path_value, root = _master(series)
    groups = qa._physical_groups(root)
    green = next(
        group for group in groups if group.get("data-plot-pen-id") == "green-0-25"
    )
    blue = next(
        group for group in groups if group.get("data-plot-pen-id") == "blue-0-25"
    )
    water = next(path for path in list(blue) if path.get("data-role") == "water")
    blue.remove(water)
    green.append(water)
    _sync_master(series, root)


def _tamper_vegetation_reserve(series: Path, _catalog: Path) -> None:
    def mutate(manifest: dict[str, Any]) -> None:
        budget = manifest["rendering"]["f1_circuit"]["context_features"][
            "vegetation_outline_budget"
        ]
        budget["configured_reserve_density_mm_per_mm2"] = 0.024

    _sync_manifest(series, mutate)


def _tamper_partial_polygon_water(series: Path, catalog_path: Path) -> None:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    model = catalog["events"][0]["circuit"]["geometry"]["model"]
    model["context"].append(
        {
            "id": "synthetic-water-polygon",
            "kind": "water",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [85.0, 15.0],
                        [95.0, 15.0],
                        [95.0, 25.0],
                        [85.0, 25.0],
                        [85.0, 15.0],
                    ]
                ],
            },
            "source_ref": "synthetic-circuit-source",
            "source_objects": ["synthetic-water-object"],
            "valid_for_season": 2026,
        }
    )
    _path_value, root = _master(series)
    blue = next(
        group
        for group in qa._physical_groups(root)
        if group.get("data-plot-pen-id") == "blue-0-25"
    )
    _path(
        blue,
        "M 108 72 L 116 72 L 116 80 L 108 80 Z",
        "context-water",
        **{
            "data-feature-id": "synthetic-water-polygon",
            "data-context-kind": "water",
            "data-source-ref": "synthetic-circuit-source",
            "data-source-object-id": "synthetic-water-object",
        },
    )
    _rebind_catalog(series, catalog_path, catalog, root=root)


def _tamper_course_fact_length(series: Path, _catalog: Path) -> None:
    def mutate(manifest: dict[str, Any]) -> None:
        manifest["rendering"]["f1_circuit"]["course_facts"]["length"]["value_m"] = 401.0

    _sync_manifest(series, mutate)


def _tamper_visible_fastest_driver(series: Path, _catalog: Path) -> None:
    def mutate(manifest: dict[str, Any]) -> None:
        facts = manifest["rendering"]["f1_circuit"]["course_facts"]
        facts["visible_groups"][2]["lines"] = [
            "1:02.345 / 2025",
            "A. LOVELACE",
        ]
        facts["visible_lines"] = [
            copy_value
            for group in facts["visible_groups"]
            for copy_value in [group["label"], *group["lines"]]
        ]

    _sync_manifest(series, mutate)


def _tamper_catalog_fastest_time(series: Path, catalog_path: Path) -> None:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    fastest = catalog["events"][0]["official_facts"]["fastest_lap"]
    fastest["time"] = "1:02.346"
    fastest["time_ms"] = 62346
    _rebind_catalog(series, catalog_path, catalog)


def _tamper_source_drift(series: Path, catalog_path: Path) -> None:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["events"][0]["circuit"]["geometry"]["model"]["lap"]["coordinates"][1][0] = (
        101.0
    )
    _write_json(catalog_path, catalog)
    index, _entry_value = _entry(series)
    index["catalog_sha256"] = _sha256(catalog_path)
    _write_json(series / "index.json", index)


def test_source_bound_transverse_host_road_crossing_is_permitted(
    tmp_path: Path,
) -> None:
    series, catalog_path = _build_release(tmp_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["events"][0]["circuit"]["geometry"]["model"]["context"][0]["geometry"][
        "coordinates"
    ] = [[50.0, -20.0], [50.0, 120.0]]
    _path_value, root = _master(series)
    host_road = next(
        path for path in root.iter(f"{SVG}path") if path.get("data-role") == "host-road"
    )
    host_road.set("d", "M 75 44 L 75 156")
    _rebind_catalog(series, catalog_path, catalog, root=root)

    report = _audit(series, catalog_path)
    assert report["technical_pass"] is True, _all_failures(report)
    metrics = report["results"][0]["metrics"]
    assert metrics["minimum_host_road_lap_clearance_mm"] == 0.0
    assert metrics["host_road_coincident_halo_sample_count"] == 0
    assert metrics["host_road_transverse_halo_sample_count"] > 0
    assert metrics["host_road_transverse_halo_length_mm"] > 0.0


@pytest.mark.parametrize(
    ("tamper", "needle"),
    (
        (_tamper_open_lap, "open by"),
        (_tamper_duplicate_turn, "turn station parity"),
        (_tamper_missing_turn, "turn station parity"),
        (_tamper_disconnected_red, "connected red hero fraction"),
        (_tamper_fake_apex, "racing apex"),
        (_tamper_drs, "DRS"),
        (_tamper_overlap, "overlaps the course corridor or pit route"),
        (_tamper_label_bbox_overlap, "label boxes"),
        (_tamper_fake_track_width, "track-width claim"),
        (_tamper_source_lineage, "not source-object bound"),
        (_tamper_visible_halo, "forbidden host-road halo path"),
        (_tamper_corridor_fallback, "course-corridor offset claim/source"),
        (_tamper_missing_corridor_group, "course-corridor path-count ledger"),
        (
            _tamper_parallel_host_road_overdraw,
            "host-road geometry overdraws the ledgered negative-space clearance",
        ),
        (_tamper_short_stroke, "below its three-nib floor"),
        (_tamper_canonical_zone, "leaves canonical zone"),
        (_tamper_visible_text, "forbidden visible <text>"),
        (_tamper_pen_order, "binding F1 order"),
        (
            _tamper_vegetation_interior_symbol,
            "vegetation interior symbols are forbidden",
        ),
        (_tamper_vegetation_wrong_pen, "Green green-0-25 is required"),
        (_tamper_water_wrong_pen, "Blue blue-0-25 is required"),
        (_tamper_vegetation_reserve, "vegetation 0.025-within-0.17 density reserve"),
        (_tamper_partial_polygon_water, "has an outline but no stipple"),
        (_tamper_course_fact_length, "course_facts.length does not exactly match"),
        (
            _tamper_visible_fastest_driver,
            "visible fastest lap does not preserve exact catalog time/full driver/year",
        ),
        (
            _tamper_catalog_fastest_time,
            "course_facts.fastest_lap does not exactly match catalog time/full driver/year",
        ),
        (_tamper_source_drift, "source geometry digest does not bind the model"),
    ),
)
def test_semantic_tampering_is_held(
    tmp_path: Path,
    tamper: Callable[[Path, Path], None],
    needle: str,
) -> None:
    series, catalog = _build_release(tmp_path)
    tamper(series, catalog)
    report = _audit(series, catalog)
    assert report["passed"] is False
    assert report["review_hold"] is True
    assert needle in _all_failures(report)


def test_format_overwrite_is_held(tmp_path: Path) -> None:
    series, catalog = _build_release(tmp_path)
    index, entry = _entry(series)
    duplicate = copy.deepcopy(entry)
    duplicate["id"] = "synthetic-grand-prix--a5-landscape"
    duplicate["format_id"] = "a5-landscape"
    index["formats"] = ["a5-portrait", "a5-landscape"]
    index["entries"].append(duplicate)
    _write_json(series / "index.json", index)
    report = _audit(series, catalog)
    assert report["passed"] is False
    failures = _all_failures(report)
    assert "overwrite/share an output path" in failures
    assert "manifest format_id drifted" in failures


def test_split_master_tampering_is_held(tmp_path: Path) -> None:
    series, catalog = _build_release(tmp_path)
    _index, entry = _entry(series)
    manifest_path = series / entry["outputs"]["manifest"]["path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    split_record = manifest["outputs"]["pen_files"][0]
    split_path = series / split_record["path"]
    split_root = ET.parse(split_path).getroot()
    path = next(split_root.iter(f"{SVG}path"))
    path.set("d", "M 12 35.122 L 20 35.122")
    ET.ElementTree(split_root).write(split_path, encoding="utf-8", xml_declaration=True)
    split_record["sha256"] = _sha256(split_path)
    _write_json(manifest_path, manifest)
    index, entry = _entry(series)
    entry["outputs"]["manifest"]["sha256"] = _sha256(manifest_path)
    _write_json(series / "index.json", index)
    report = _audit(series, catalog)
    assert report["passed"] is False
    assert "differs from its master group" in _all_failures(report)
