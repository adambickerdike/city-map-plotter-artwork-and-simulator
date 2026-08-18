from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import __version__
from .batch import (
    build_batch_plan,
    default_report_path,
    execute_batch_plan,
    file_sha256,
)
from .catalog import (
    SUBJECT_KINDS,
    Catalog,
    CatalogSubject,
    load_catalog,
    select_subjects,
    subject_record,
)
from .cartography import (
    DETAIL_PROFILE_CHOICES,
    FULL_CARTOGRAPHY_DETAIL_PROFILES,
    WATER_FILL_CHOICES,
)
from .features import extract_features, highway_coverage
from .geometry import (
    POSTER_ORIENTATIONS,
    POSTER_PRESETS,
    crop_bbox_to_aspect,
    expand_bbox_to_aspect,
    load_plate_format,
    make_layout,
    make_poster_layout,
    pad_bbox,
    plate_format_id_for_preset,
    plate_nib_ladder_mm,
    poster_plate_format_id,
    poster_sheet_name,
)
from .models import BoundingBox, MapPlotterError
from .osm import (
    DEFAULT_NOMINATIM_URL,
    DEFAULT_OVERPASS_URL,
    canonical_landmark_refs,
    default_cache_dir,
    fetch_overpass,
    geocode_place,
    load_overpass_file,
    user_agent_from,
)
from .pbf import load_pbf
from .pens import (
    ACTUAL_NIB_LADDER_MM,
    ACTUAL_PENS_PROFILE,
    PEN_PROFILE_CHOICES,
    PEN_PROFILE_STYLE,
    load_pen_inventory,
    resolve_pen_inventory,
    write_pen_calibration_svg,
)
from .physical import ROAD_LAYERS, ROAD_STYLE_CHOICES
from .styles import DEFAULT_FAMILIES, enabled_layer_ids, load_styles, parse_families
from .svg import POSTER_LAYOUT_CHOICES, render_svg, write_manifest, write_pen_svgs
from .themes import (
    expand_theme_export_args,
    load_theme,
    load_theme_catalog,
    load_theme_file,
    resolve_subject_copy,
    resolve_theme_styles,
    resolved_theme_contract,
)


def _normalise_production_source_timestamp(value: object) -> str:
    """Validate and canonicalize a source snapshot timestamp for release use."""

    if not isinstance(value, str) or not value.strip():
        raise MapPlotterError(
            "A themed production release requires a non-empty RFC 3339 source "
            "snapshot timestamp."
        )
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(
            candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        )
    except ValueError as exc:
        raise MapPlotterError(
            "A themed production release requires a valid RFC 3339 source "
            f"snapshot timestamp, not {value!r}."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise MapPlotterError(
            "A themed production source timestamp must explicitly use UTC."
        )
    resolved = parsed.astimezone(UTC)
    if resolved > datetime.now(UTC) + timedelta(minutes=5):
        raise MapPlotterError(
            "A themed production source timestamp cannot be in the future."
        )
    return resolved.isoformat(timespec="seconds").replace("+00:00", "Z")


def _same_file(left: Path, right: Path) -> bool:
    """Compare paths, including existing hard-link aliases."""

    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return left.resolve() == right.resolve()


def _pen_output_paths(
    master_path: Path,
    pen_sequence: Sequence[dict[str, Any]],
    *,
    output_dir: Path | None,
) -> list[Path]:
    """Return the exact final paths used by ``write_pen_svgs``."""

    destination = output_dir or master_path.parent
    paths: list[Path] = []
    for pen in pen_sequence:
        step = int(pen["step"])
        slug = re.sub(
            r"[^a-z0-9]+",
            "-",
            str(pen.get("pen_id") or f"{pen['ink']}-{pen['nib_mm']}").casefold(),
        ).strip("-")
        paths.append(destination / f"{master_path.stem}.pen-{step:02d}-{slug}.svg")
    return paths


def _preflight_export_paths(
    master_path: Path,
    manifest_path: Path,
    *,
    pen_paths: Sequence[Path] = (),
    protected_paths: Sequence[Path] = (),
) -> None:
    """Reject output collisions, including existing hard-link aliases."""

    outputs = [
        ("master SVG", master_path),
        ("plot manifest", manifest_path),
        *((f"per-pen SVG {index}", path) for index, path in enumerate(pen_paths, 1)),
    ]
    for index, (left_label, left_path) in enumerate(outputs):
        for right_label, right_path in outputs[index + 1 :]:
            if _same_file(left_path, right_path):
                raise MapPlotterError(
                    f"{left_label} path {left_path} collides with "
                    f"{right_label} path {right_path}."
                )
        for protected_path in protected_paths:
            if _same_file(left_path, protected_path):
                raise MapPlotterError(
                    f"{left_label} path {left_path} would overwrite input file "
                    f"{protected_path}."
                )


def _publish_staged_svg(staged_path: Path, output_path: Path) -> None:
    """Atomically publish a staged master SVG on the destination filesystem."""

    temporary = output_path.with_suffix(output_path.suffix + f".{os.getpid()}.tmp")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged_path, temporary)
        os.replace(temporary, output_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MapPlotterError(f"Could not write SVG {output_path}: {exc}") from exc


def _png_dimensions(path: Path) -> tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError as exc:
        raise MapPlotterError(f"Could not read PNG {path}: {exc}") from exc
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        raise MapPlotterError(f"Rasterizer did not produce a valid PNG at {path}.")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def _svg_page_mm(path: Path) -> tuple[float, float]:
    try:
        root = ET.parse(path).getroot()
        width = root.attrib["width"]
        height = root.attrib["height"]
        if not width.endswith("mm") or not height.endswith("mm"):
            raise ValueError("page width and height are not expressed in millimetres")
        return float(width[:-2]), float(height[:-2])
    except (OSError, ET.ParseError, KeyError, ValueError) as exc:
        raise MapPlotterError(
            f"Could not read SVG page size from {path}: {exc}"
        ) from exc


def _rasterize_png(svg_path: Path, png_path: Path, dpi: float) -> dict[str, Any]:
    executable = shutil.which("inkscape")
    if executable is None:
        raise MapPlotterError(
            "PNG export requires Inkscape on PATH; install Inkscape or omit --png."
        )
    temporary = png_path.with_name(f".{png_path.name}.{os.getpid()}.tmp.png")
    temporary.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [
                executable,
                str(svg_path),
                "--export-type=png",
                "--export-area-page",
                f"--export-dpi={dpi:g}",
                "--export-background=white",
                "--export-background-opacity=255",
                f"--export-filename={temporary}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise MapPlotterError(
                f"Inkscape PNG export failed for {svg_path}: "
                f"{detail or f'exit status {result.returncode}'}"
            )
        width_px, height_px = _png_dimensions(temporary)
        width_mm, height_mm = _svg_page_mm(svg_path)
        expected = (
            round(width_mm * dpi / 25.4),
            round(height_mm * dpi / 25.4),
        )
        if (width_px, height_px) != expected:
            raise MapPlotterError(
                f"PNG {temporary} is {width_px}x{height_px}px; expected "
                f"{expected[0]}x{expected[1]}px at {dpi:g} DPI."
            )
        png_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, png_path)
        version = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return {
            "format": "PNG",
            "path": str(png_path.resolve()),
            "dpi": dpi,
            "width_px": width_px,
            "height_px": height_px,
            "background": "opaque white",
            "renderer": version or "Inkscape",
            "source_svg_sha256": file_sha256(svg_path),
            "png_sha256": file_sha256(png_path),
        }
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot",
        description="Compile OpenStreetMap vectors into a paper-sized, pen-aware SVG.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export",
        help="Download or load map vectors and render an SVG.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )

    extent = export.add_mutually_exclusive_group(required=True)
    extent.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Longitude/latitude bounds in WEST SOUTH EAST NORTH order, e.g. -0.14 51.50 -0.12 51.51.",
    )
    extent.add_argument(
        "--center",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        help="Center in LATITUDE LONGITUDE order; must be combined with --radius-km.",
    )
    extent.add_argument("--place", help="One deliberate place search via Nominatim.")
    export.add_argument(
        "--country-code",
        help="Optional two-letter country filter for --place disambiguation.",
    )
    extent.add_argument(
        "--subject",
        help="Bundled catalog subject ID; use 'mapplot catalog list' to discover IDs.",
    )
    export.add_argument(
        "--radius-km",
        type=float,
        help=(
            "Radius around --center, --place, or --subject; catalog defaults or "
            "a bounded 5 km standard / 2.5 km A5 place radius are used if omitted."
        ),
    )
    export.add_argument(
        "--catalog-file",
        type=Path,
        help="Optional version-1 catalog JSON to use instead of the bundled catalog.",
    )
    input_source = export.add_mutually_exclusive_group()
    input_source.add_argument(
        "--input-json",
        type=Path,
        help="Use a saved Overpass API JSON response covering the bbox instead of downloading.",
    )
    input_source.add_argument(
        "--input-pbf",
        type=Path,
        help="Read a local .osm.pbf with PyOsmium instead of using Overpass (install the optional 'pbf' dependency).",
    )
    export.add_argument(
        "--output", "-o", type=Path, required=True, help="Output master SVG path."
    )
    export.add_argument(
        "--theme",
        metavar="THEME_ID",
        help=(
            "Versioned design contract. Theme-owned rendering, palette, "
            "typography, copy, and placement options cannot be overridden. "
            f"Packaged themes: {', '.join(sorted(load_theme_catalog()))}."
        ),
    )
    export.add_argument(
        "--theme-file",
        type=Path,
        help=(
            "Read the design contract from a theme file you wrote instead of the "
            "packaged catalog. Add --theme to pick one theme out of a file that "
            "defines several."
        ),
    )
    export.add_argument(
        "--preset",
        choices=("standard", *sorted(POSTER_PRESETS)),
        default="standard",
        help=(
            "Composition and paper size. The a5-* presets plot A5, the a4-* "
            "presets plot A4; 'clean' is the restrained selection and "
            "'balanced' the richer one. Each poster preset takes its zones, "
            "type scale and nib ladder from the matching plate format, so A4 "
            "gets A4 pens. Add --orientation landscape for the rail "
            "composition. 'standard' is the free-form non-poster export."
        ),
    )
    export.add_argument(
        "--detail-profile",
        choices=tuple(sorted(DETAIL_PROFILE_CHOICES)),
        default="faithful",
        help=(
            "faithful preserves every qualifying source feature; "
            "plotter-faithful keeps the same cartographic selection but omits "
            "only residual sub-nib physical marks; ink-balanced starts from "
            "that same full cartography and permits a later verified ink-budget "
            "gate; plot applies selective poster cleanup."
        ),
    )
    export.add_argument(
        "--extent-fit",
        choices=("contain", "cover"),
        default="contain",
        help=(
            "For A5 posters, expand the extent to keep everything or centre-crop "
            "it to fill the canonical map field."
        ),
    )
    export.add_argument(
        "--manifest", type=Path, help="Manifest path; defaults beside the SVG."
    )
    export.add_argument(
        "--layers",
        default=",".join(DEFAULT_FAMILIES),
        help="Comma-separated families: roads, water, railways, parks, buildings, boundaries.",
    )
    export.add_argument(
        "--style", type=Path, help="JSON style overrides; see examples/style.json."
    )
    export.add_argument(
        "--pen-profile",
        choices=tuple(sorted(PEN_PROFILE_CHOICES)),
        default=ACTUAL_PENS_PROFILE,
        help=(
            "Resolve requested widths against a named physical pen inventory; "
            "'style' preserves explicit style-file nibs."
        ),
    )
    export.add_argument(
        "--pen-inventory",
        type=Path,
        help=(
            "Custom inventory JSON with nominal and optionally measured effective "
            "widths; when present it replaces the named pen profile."
        ),
    )
    export.add_argument(
        "--stock-id",
        help=(
            "Stable paper/stock identifier used to match measured pen widths, "
            "for example 'hahnemuhle-bamboo-290-white'."
        ),
    )
    export.add_argument(
        "--stock-tone",
        choices=("light", "mid", "dark"),
        default="light",
        help="Paper tone used for white-ink visibility safety checks.",
    )
    export.add_argument(
        "--pen-down-speed",
        help=(
            "Exact plotter pen-down speed setting. Production output requires it "
            "to match every selected pen's calibration record."
        ),
    )
    export.add_argument(
        "--production",
        action="store_true",
        help=(
            "Fail unless every selected pen is measured on --stock-id and all "
            "physical-media safety gates pass. Themed releases also require a "
            "pinned, timestamped --input-pbf whose header bounds cover the "
            "acquisition extent; otherwise exports are review-only."
        ),
    )
    export.add_argument(
        "--paper",
        help=(
            "A5–A0, Letter, Legal, or custom; standard defaults to A4. Poster "
            "presets carry their own sheet, so this only confirms it."
        ),
    )
    export.add_argument(
        "--orientation",
        choices=("portrait", "landscape", "auto"),
        help=(
            "Poster presets default to portrait (the stack composition); "
            "landscape selects the rail composition, with the field on the "
            "left and all copy in a right-hand rail."
        ),
    )
    export.add_argument(
        "--width-mm", type=float, help="Width used with --paper custom."
    )
    export.add_argument(
        "--height-mm", type=float, help="Height used with --paper custom."
    )
    export.add_argument(
        "--margin-mm",
        type=float,
        help="Unplotted paper margin; standard defaults to 10.",
    )
    export.add_argument(
        "--simplify-mm",
        type=float,
        help=(
            "Maximum page-space geometry error; defaults to 0.04 mm for the "
            "full-cartography profiles (faithful, plotter-faithful, and "
            "ink-balanced), and 0.08 mm for plot. Use 0 for exact source vertices."
        ),
    )
    export.add_argument(
        "--frame", action="store_true", help="Draw the exact mapped-area frame."
    )
    export.add_argument(
        "--attribution-mode",
        choices=("embedded", "external"),
        default="embedded",
        help=(
            "Draw attribution on the sheet, or record the operator-declared "
            "external placement for clean artwork. External credit must actually "
            "accompany public output."
        ),
    )
    export.add_argument(
        "--external-attribution-placement",
        help=(
            "Where the required OpenStreetMap credit will appear when "
            "--attribution-mode external is selected."
        ),
    )
    export.add_argument(
        "--scale-bar",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Draw the physically scaled distance bar; the north mark is retained.",
    )
    export.add_argument(
        "--scale-detail",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include the automatic APPROX SCALE line in poster details.",
    )
    export.add_argument(
        "--road-style",
        choices=tuple(sorted(ROAD_STYLE_CHOICES)),
        default=None,
        help=(
            "Use real parallel nib-offset road weights, or single centrelines. "
            "Defaults to centreline for faithful, plotter-faithful, and "
            "ink-balanced, and multi for plot."
        ),
    )
    export.add_argument(
        "--nib-mm",
        type=float,
        help="Target black nib for --road-style single-nib; defaults to 0.25 mm.",
    )
    export.add_argument(
        "--optimise",
        "--optimize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reorder and safely reverse map paths to reduce pen-up travel.",
    )
    export.add_argument(
        "--physical-audit",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Scan emitted marks for below-nib separation conflicts; defaults "
            "on for faithful exports and off for plot exports."
        ),
    )
    export.add_argument(
        "--accept-physical-conflicts",
        action="store_true",
        help=(
            "After inspecting a complete physical audit, explicitly accept "
            "reported pairs of pen marks that may merge. This does not accept "
            "sub-nib trails or incomplete scans."
        ),
    )
    export.add_argument(
        "--allow-repeat-passes",
        action="store_true",
        help=(
            "Approve intentional repeat-over-the-same-line passes for ink density; "
            "repeat passes never increase plotted width."
        ),
    )
    export.add_argument(
        "--split-by-pen",
        action="store_true",
        help="Also write one page-sized SVG for every physical pen step.",
    )
    export.add_argument(
        "--pen-output-dir",
        type=Path,
        help="Directory for --split-by-pen files; defaults beside the master SVG.",
    )
    export.add_argument(
        "--title", help="SVG title; defaults to the place or output filename."
    )
    export.add_argument(
        "--subtitle",
        help="Single-stroke subtitle used by poster presets.",
    )
    export.add_argument(
        "--poster-layout",
        choices=tuple(sorted(POSTER_LAYOUT_CHOICES)),
        default="classic",
        help=(
            "Poster copy composition. University memorabilia places a serif city "
            "title and coordinates in the header, with writable personal fields "
            "in the footer."
        ),
    )
    export.add_argument(
        "--water-fill",
        choices=tuple(sorted(WATER_FILL_CHOICES)),
        default="none",
        help="Optional plotter-native surface-water pattern.",
    )
    export.add_argument(
        "--landmark-buildings",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Draw only physically prominent churches, cathedrals, university, "
            "stadium, civic, cultural, hospital, and station footprints."
        ),
    )
    export.add_argument(
        "--landmark-ref",
        action="append",
        default=[],
        metavar="TYPE/ID",
        help=(
            "Require one exact OSM building footprint, using canonical way/123 "
            "or relation/456 syntax. Repeat for multiple reviewed landmarks."
        ),
    )
    export.add_argument(
        "--person-name",
        help="Optional name printed in the university memorabilia footer.",
    )
    export.add_argument(
        "--degree",
        help="Optional degree printed in the university memorabilia footer.",
    )
    export.add_argument(
        "--honours",
        help="Optional honours classification printed in the memorabilia footer.",
    )
    export.add_argument(
        "--years",
        help="Optional attendance years printed in the memorabilia footer.",
    )
    export.add_argument(
        "--detail",
        action="append",
        default=[],
        help="Poster detail line; repeat up to three times.",
    )
    export.add_argument(
        "--max-area-km2",
        type=float,
        default=500.0,
        help="Safety limit for public Overpass requests (default: 500).",
    )
    export.add_argument(
        "--allow-large-area",
        action="store_true",
        help="Override the public-service area safety check.",
    )
    export.add_argument(
        "--cache-dir",
        type=Path,
        default=default_cache_dir(),
        help="Service response cache.",
    )
    export.add_argument(
        "--refresh", action="store_true", help="Ignore cached service responses."
    )
    export.add_argument(
        "--timeout", type=int, default=120, help="HTTP timeout in seconds."
    )
    export.add_argument(
        "--user-agent", help="Identifying application User-Agent for OSM services."
    )
    export.add_argument("--overpass-url", default=DEFAULT_OVERPASS_URL)
    export.add_argument("--nominatim-url", default=DEFAULT_NOMINATIM_URL)

    catalog = commands.add_parser(
        "catalog",
        help="Discover curated university and marathon map subjects.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    catalog.add_argument(
        "--catalog-file",
        type=Path,
        help="Optional version-1 catalog JSON to use instead of the bundled catalog.",
    )
    catalog_actions = catalog.add_subparsers(dest="catalog_action", required=True)
    collections = catalog_actions.add_parser(
        "collections",
        help="List catalog collections and their selection rules.",
        allow_abbrev=False,
    )
    collections.add_argument("--json", action="store_true", help="Emit JSON.")
    listing = catalog_actions.add_parser(
        "list", help="List map subjects.", allow_abbrev=False
    )
    listing.add_argument("--collection", help="Restrict to one collection ID.")
    listing.add_argument(
        "--kind", choices=tuple(sorted(SUBJECT_KINDS)), help="Restrict by subject type."
    )
    listing.add_argument(
        "--country", metavar="CODE", help="Restrict by two-letter ISO country code."
    )
    listing.add_argument("--json", action="store_true", help="Emit JSON.")
    show = catalog_actions.add_parser(
        "show", help="Show one subject and its provenance.", allow_abbrev=False
    )
    show.add_argument("subject_id")
    batch = catalog_actions.add_parser(
        "export",
        help="Render one catalog collection, or every collection, resumably.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    selection = batch.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--collection",
        action="append",
        help="Catalog collection ID to export; repeat to combine collections.",
    )
    selection.add_argument(
        "--all-collections",
        action="store_true",
        help="Export all catalog collections in catalog order.",
    )
    batch.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Root directory for deterministic collection/subject outputs.",
    )
    batch.add_argument(
        "--report",
        type=Path,
        help="Batch JSON report; defaults under --output-dir.",
    )
    batch.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the complete plan without writing or downloading.",
    )
    batch.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume a matching report and skip hash-verified artifacts.",
    )
    batch.add_argument(
        "--overwrite",
        action="store_true",
        help="Start a new report and replace artifacts at planned paths.",
    )
    batch.add_argument(
        "--keep-going",
        action="store_true",
        help="Record an export failure and continue with later subjects.",
    )
    batch.add_argument(
        "--delay-seconds",
        type=float,
        default=2.0,
        help="Delay between public-service exports; local PBF batches skip it.",
    )
    batch.add_argument(
        "--limit",
        type=int,
        help="Export only the first N planned subjects, useful for a pilot run.",
    )
    batch.add_argument(
        "--title-mode",
        choices=("subject", "city"),
        default="subject",
        help="Use each catalog subject name or its city as the plotted title.",
    )
    batch.add_argument(
        "--png",
        action="store_true",
        help="Also render an opaque white, page-sized PNG beside every master SVG.",
    )
    batch.add_argument(
        "--png-dpi",
        type=float,
        default=254.0,
        help="Raster resolution used with --png (254 DPI gives A5 at 1480x2100).",
    )
    batch.add_argument(
        "--export-args",
        nargs=argparse.REMAINDER,
        default=[],
        metavar="OPTION",
        help=(
            "Ordinary 'mapplot export' flags shared by every item; this must be "
            "the final batch option. Subject, extent, title, input JSON, and "
            "output paths remain batch-controlled."
        ),
    )

    pens = commands.add_parser(
        "pens",
        help="Inspect and calibrate physical pen inventories.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    pen_actions = pens.add_subparsers(dest="pens_action", required=True)
    calibration = pen_actions.add_parser(
        "calibration",
        help="Write an A3 card with ten independent 100 mm one-pass specimens per pen.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    calibration.add_argument("--output", "-o", type=Path, required=True)
    calibration.add_argument(
        "--manifest",
        type=Path,
        help="Calibration manifest path; defaults beside the SVG.",
    )
    calibration.add_argument(
        "--pen-profile",
        choices=tuple(sorted(PEN_PROFILE_CHOICES - {PEN_PROFILE_STYLE})),
        default=ACTUAL_PENS_PROFILE,
    )
    calibration.add_argument(
        "--pen-inventory",
        type=Path,
        help="Calibrate a custom inventory JSON instead of the named profile.",
    )
    calibration.add_argument(
        "--stock-id",
        required=True,
        help="Stable identifier for the exact paper stock used for this run.",
    )
    calibration.add_argument(
        "--stock-tone",
        choices=("light", "mid", "dark"),
        required=True,
        help="Paper tone used to select only plausibly visible calibration pens.",
    )
    calibration.add_argument(
        "--pen-down-speed",
        required=True,
        help=(
            "Exact plotter pen-down speed setting to preserve with the measurements, "
            "for example 'axidraw-25-percent'."
        ),
    )
    return parser


def _resolve_bbox(
    args: argparse.Namespace,
    user_agent: str | None,
    subject: CatalogSubject | None = None,
) -> tuple[BoundingBox, str | None]:
    if args.bbox:
        return BoundingBox(*args.bbox), None
    if args.center:
        if args.radius_km is None:
            raise MapPlotterError("--center requires --radius-km.")
        latitude, longitude = args.center
        return BoundingBox.around(latitude, longitude, args.radius_km), None
    if subject is not None:
        radius_km = (
            args.radius_km if args.radius_km is not None else subject.preview_radius_km
        )
        return (
            BoundingBox.around(subject.latitude, subject.longitude, radius_km),
            subject.name,
        )
    if user_agent is None:
        raise MapPlotterError(
            "A User-Agent is required to resolve --place through Nominatim."
        )
    _place_bbox, center, display_name = geocode_place(
        args.place,
        endpoint=args.nominatim_url,
        user_agent=user_agent,
        cache_dir=args.cache_dir,
        timeout_s=min(args.timeout, 60),
        refresh=args.refresh,
        country_code=args.country_code,
    )
    if args.radius_km is not None:
        return BoundingBox.around(center[0], center[1], args.radius_km), display_name
    # Nominatim often returns an administrative boundary for a city name.  A
    # bounded settlement-scale crop is a safer default for plotting than an
    # entire municipality, county, or metropolitan authority.
    default_radius_km = 2.5 if args.preset in POSTER_PRESETS else 5.0
    return (
        BoundingBox.around(center[0], center[1], default_radius_km),
        display_name,
    )


def _resolved_road_style(detail_profile: str, requested: str | None) -> str:
    if requested is not None:
        return requested
    return (
        "centreline" if detail_profile in FULL_CARTOGRAPHY_DETAIL_PROFILES else "multi"
    )


def _resolved_simplify_mm(detail_profile: str, requested: float | None) -> float:
    if requested is not None:
        return requested
    return 0.04 if detail_profile in FULL_CARTOGRAPHY_DETAIL_PROFILES else 0.08


def _poster_plate_selection(args: argparse.Namespace) -> str | None:
    """Resolve which of the six plates a poster export is asking for.

    The sheet comes from the preset name and the archetype from
    ``--orientation``; both are named explicitly by the operator so no export
    can silently inherit another sheet's zones or nib ladder.
    """

    if args.preset not in POSTER_PRESETS:
        return None
    sheet = poster_sheet_name(args.preset)
    if args.paper is not None and args.paper.upper() != sheet:
        alternative = f"{args.paper.casefold()}-{args.preset.split('-', 1)[1]}"
        suffix = (
            f" Use --preset {alternative} for {args.paper.upper()}."
            if alternative in POSTER_PRESETS
            else ""
        )
        raise MapPlotterError(
            f"Poster preset {args.preset!r} plots on {sheet}, but --paper "
            f"{args.paper.upper()} was requested.{suffix}"
        )
    if args.orientation is not None and args.orientation not in POSTER_ORIENTATIONS:
        raise MapPlotterError(
            f"Poster presets require --orientation {' or '.join(POSTER_ORIENTATIONS)}."
        )
    if args.width_mm is not None or args.height_mm is not None:
        raise MapPlotterError(
            "A fixed poster plate does not accept custom paper dimensions."
        )
    format_id = poster_plate_format_id(args.preset, orientation=args.orientation)
    assert format_id is not None
    safe_margin_mm = float(load_plate_format(format_id)["safe_margin_mm"])
    if args.margin_mm is not None and args.margin_mm != safe_margin_mm:
        raise MapPlotterError(
            f"Plate {format_id} uses a fixed {safe_margin_mm:g} mm plotter-safe border."
        )
    return format_id


def _run_export(args: argparse.Namespace) -> int:
    if args.theme_file is not None:
        theme = load_theme_file(args.theme_file, theme_id=args.theme)
    elif args.theme is not None:
        theme = load_theme(args.theme)
    else:
        theme = None
    poster_format_id = _poster_plate_selection(args)
    preset_format_id = poster_format_id or plate_format_id_for_preset(args.preset)
    if (
        theme is not None
        and preset_format_id is not None
        and theme.format_id != preset_format_id
    ):
        raise MapPlotterError(
            f"Theme {theme.id!r} uses plate format {theme.format_id!r}, but preset "
            f"{args.preset!r} is bound to {preset_format_id!r}."
        )
    road_style = _resolved_road_style(args.detail_profile, args.road_style)
    if len(args.detail) > 3:
        raise MapPlotterError("--detail may be repeated at most three times.")
    personalisation_values = (
        args.person_name,
        args.degree,
        args.honours,
        args.years,
    )
    if args.poster_layout != "university-memorabilia" and any(personalisation_values):
        raise MapPlotterError(
            "Personalisation fields require --poster-layout university-memorabilia."
        )
    if args.poster_layout == "university-memorabilia":
        if args.preset not in POSTER_PRESETS:
            raise MapPlotterError(
                "--poster-layout university-memorabilia requires an A5 poster preset."
            )
        if theme is not None:
            raise MapPlotterError(
                "University memorabilia is a new composition and cannot be mixed "
                "with an existing versioned theme contract."
            )
        # The memorabilia compass has its own clear header zone, so the legacy
        # map-overlay furniture is deliberately disabled.
        args.scale_bar = False
    if args.pen_output_dir is not None and not args.split_by_pen:
        raise MapPlotterError("--pen-output-dir requires --split-by-pen.")
    if args.attribution_mode == "external":
        if not (
            args.external_attribution_placement
            and args.external_attribution_placement.strip()
        ):
            raise MapPlotterError(
                "--attribution-mode external requires --external-attribution-placement."
            )
    elif args.external_attribution_placement is not None:
        raise MapPlotterError(
            "--external-attribution-placement requires --attribution-mode external."
        )
    if args.country_code is not None and args.place is None:
        raise MapPlotterError("--country-code requires --place.")
    if args.nib_mm is not None and road_style != "single-nib":
        raise MapPlotterError("--nib-mm requires --road-style single-nib.")
    if args.nib_mm is not None and (not isfinite(args.nib_mm) or args.nib_mm <= 0):
        raise MapPlotterError("--nib-mm must be greater than zero.")
    pen_inventory = (
        load_pen_inventory(args.pen_inventory)
        if args.pen_inventory is not None
        else resolve_pen_inventory(args.pen_profile)
    )
    # The sheet being drawn owns the ladder its layers may use; the studio
    # ladder is the fallback for free-form exports that bind no plate.
    allowed_nibs_mm = (
        plate_nib_ladder_mm(preset_format_id) or ACTUAL_NIB_LADDER_MM
        if pen_inventory is not None
        else None
    )
    if args.preset not in POSTER_PRESETS and args.extent_fit != "contain":
        raise MapPlotterError("--extent-fit cover currently applies to poster presets.")
    families = parse_families(args.layers)
    landmark_refs = canonical_landmark_refs(args.landmark_ref)
    if args.water_fill == "dots" and "water" not in families:
        raise MapPlotterError("--water-fill dots requires the water family.")
    if args.landmark_buildings and "buildings" not in families:
        raise MapPlotterError(
            "--landmark-buildings requires the buildings family in --layers."
        )
    if landmark_refs and not args.landmark_buildings:
        raise MapPlotterError("--landmark-ref requires --landmark-buildings.")
    if landmark_refs and "buildings" not in families:
        raise MapPlotterError(
            "--landmark-ref requires the buildings family in --layers."
        )
    if landmark_refs and args.input_pbf is not None:
        raise MapPlotterError(
            "--landmark-ref cannot be used with --input-pbf because the filtered "
            "PBF reader cannot distinguish an absent object from an ineligible "
            "non-building object. Use live Overpass or a saved Overpass JSON source."
        )
    catalog_subject = (
        load_catalog(args.catalog_file).subject(args.subject)
        if args.subject is not None
        else None
    )
    if theme is not None and catalog_subject is None:
        raise MapPlotterError(
            f"Theme {theme.id!r} requires --subject so its purpose-specific "
            "title, subtitle, and detail policy cannot be bypassed."
        )
    if (
        catalog_subject is not None
        and catalog_subject.is_city_preview_only
        and args.radius_km is None
    ):
        raise MapPlotterError(
            f"Marathon subject {catalog_subject.id!r} has no verified course geometry. "
            "Provide --radius-km explicitly to export a city-basemap preview, or import an official route extent first."
        )
    if args.radius_km is not None and args.bbox is not None:
        raise MapPlotterError(
            "--radius-km only applies to --center, --place, or --subject."
        )
    simplify_mm = _resolved_simplify_mm(args.detail_profile, args.simplify_mm)
    if not isfinite(simplify_mm) or simplify_mm < 0:
        raise MapPlotterError("--simplify-mm cannot be negative.")
    if args.timeout <= 0:
        raise MapPlotterError("--timeout must be greater than zero.")
    if not isfinite(args.max_area_km2) or args.max_area_km2 <= 0:
        raise MapPlotterError("--max-area-km2 must be greater than zero.")
    manifest_path = args.manifest or args.output.with_suffix(".plot.json")
    protected_inputs = [
        path
        for path in (
            args.input_json,
            args.input_pbf,
            args.style,
            args.catalog_file,
            args.pen_inventory,
        )
        if path is not None
    ]
    if theme is not None and args.production and args.input_pbf is None:
        raise MapPlotterError(
            "A themed production release requires a pinned --input-pbf source. "
            "Live Overpass and saved JSON are preview-only because they do not "
            "prove that one immutable extract covers every requested family and "
            "cohort extent."
        )
    _preflight_export_paths(
        args.output,
        manifest_path,
        protected_paths=protected_inputs,
    )
    needs_network = (
        args.input_json is None and args.input_pbf is None
    ) or args.place is not None
    user_agent = user_agent_from(args.user_agent) if needs_network else None
    bbox, resolved_place = _resolve_bbox(args, user_agent, catalog_subject)
    if args.preset in POSTER_PRESETS:
        if preset_format_id is None:
            raise MapPlotterError(
                f"Poster preset {args.preset!r} has no binding plate format."
            )
        map_field_aspect = float(
            load_plate_format(preset_format_id)["map_field_aspect"]
        )
        bbox = (
            expand_bbox_to_aspect(bbox, map_field_aspect)
            if args.extent_fit == "contain"
            else crop_bbox_to_aspect(bbox, map_field_aspect)
        )
    acquisition_bbox = pad_bbox(bbox) if args.input_json is None else bbox
    if (
        args.input_json is None
        and args.input_pbf is None
        and acquisition_bbox.approximate_area_km2 > args.max_area_km2
        and not args.allow_large_area
    ):
        raise MapPlotterError(
            f"Selected area is approximately {acquisition_bbox.approximate_area_km2:.0f} km², above the "
            f"{args.max_area_km2:.0f} km² public-service safety limit. Choose a tighter bbox or "
            "radius, use --allow-large-area knowingly, or move to a local .osm.pbf workflow."
        )

    selected = enabled_layer_ids(families)
    styles = (
        resolve_theme_styles(theme, selected)
        if theme is not None
        else load_styles(
            args.style,
            selected,
            preset=args.preset,
            format_id=preset_format_id,
        )
    )
    if road_style == "single-nib":
        road_nib_mm = args.nib_mm if args.nib_mm is not None else 0.25
        styles = [
            replace(
                style,
                pen=f"Black {road_nib_mm:g}",
                ink="Black",
                nib_mm=road_nib_mm,
                stroke_width_mm=road_nib_mm,
            )
            if style.id in ROAD_LAYERS
            else style
            for style in styles
        ]
    if args.preset in POSTER_PRESETS:
        assert poster_format_id is not None
        layout = make_poster_layout(
            bbox, preset=args.preset, format_id=poster_format_id
        )
    else:
        layout = make_layout(
            bbox,
            paper_name=args.paper or "A4",
            orientation=args.orientation or "auto",
            margin_mm=args.margin_mm if args.margin_mm is not None else 10.0,
            width_mm=args.width_mm,
            height_mm=args.height_mm,
        )

    design_contract = None
    if theme is not None:
        if pen_inventory is None:
            raise MapPlotterError(
                f"Theme {theme.id!r} requires a concrete physical pen inventory."
            )
        expected_page = load_plate_format(theme.format_id)
        if (
            layout.page.name.upper() != str(expected_page["sheet"]).upper()
            or layout.page.orientation != expected_page["orientation"]
        ):
            raise MapPlotterError(
                f"Theme {theme.id!r} requires plate format {theme.format_id!r}."
            )
        design_contract = resolved_theme_contract(
            theme,
            styles=styles,
            inventory=pen_inventory,
            stock_tone=args.stock_tone,
        )

    if args.input_json is not None:
        acquisition = load_overpass_file(args.input_json)
    elif args.input_pbf is not None:
        acquisition = load_pbf(
            args.input_pbf,
            acquisition_bbox,
            {style.id for style in styles},
        )
    else:
        assert user_agent is not None
        acquisition = fetch_overpass(
            acquisition_bbox,
            families,
            endpoint=args.overpass_url,
            user_agent=user_agent,
            cache_dir=args.cache_dir,
            timeout_s=args.timeout,
            refresh=args.refresh,
            landmark_buildings_only=args.landmark_buildings,
            landmark_refs=landmark_refs,
        )

    canonical_source_json = json.dumps(
        acquisition.data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    acquisition.source_metadata["canonical_source_data_sha256"] = hashlib.sha256(
        canonical_source_json
    ).hexdigest()
    acquisition.source_metadata["acquisition_mode"] = (
        "pinned-json"
        if args.input_json is not None
        else "pinned-pbf"
        if args.input_pbf is not None
        else "live-overpass"
    )
    acquisition.source_metadata["source_pinned"] = (
        args.input_json is not None or args.input_pbf is not None
    )
    embedded_acquisition = acquisition.data.get("mapplot_acquisition")
    if isinstance(embedded_acquisition, dict):
        acquisition.source_metadata["embedded_acquisition"] = embedded_acquisition
    landmark_ref_acquisition = acquisition.source_metadata.setdefault(
        "landmark_ref_acquisition", {}
    )
    if not isinstance(landmark_ref_acquisition, dict):
        raise MapPlotterError(
            "Landmark-ref acquisition provenance must be a JSON object."
        )
    landmark_ref_acquisition.update(
        {
            "schema_version": 1,
            "requested_refs": list(landmark_refs),
            "acquisition_mode": acquisition.source_metadata["acquisition_mode"],
            "embedded_landmark_ref_acquisition": (
                embedded_acquisition.get("landmark_ref_acquisition")
                if isinstance(embedded_acquisition, dict)
                else None
            ),
        }
    )
    if acquisition.query is not None:
        acquisition.source_metadata["overpass_query"] = acquisition.query
        acquisition.source_metadata["overpass_query_sha256"] = hashlib.sha256(
            acquisition.query.encode("utf-8")
        ).hexdigest()

    protected_sources = list(protected_inputs)
    declared_source_path = args.input_json or args.input_pbf
    source_path = (
        declared_source_path
        if declared_source_path is not None
        else Path(acquisition.cache_path)
        if acquisition.cache_path is not None
        else None
    )
    if source_path is not None:
        acquisition.source_metadata["source_file_sha256"] = file_sha256(source_path)
        protected_sources.append(source_path)
        _preflight_export_paths(
            args.output,
            manifest_path,
            protected_paths=(source_path,),
        )

    source_osm3s = acquisition.data.get("osm3s")
    source_timestamp = (
        source_osm3s.get("timestamp_osm_base")
        if isinstance(source_osm3s, dict)
        else None
    ) or acquisition.source_metadata.get("source_timestamp")
    normalized_source_timestamp: str | None = None
    try:
        normalized_source_timestamp = _normalise_production_source_timestamp(
            source_timestamp
        )
    except MapPlotterError:
        pass
    source_timestamp_kind = acquisition.source_metadata.get("source_timestamp_kind")
    coverage = acquisition.source_metadata.get("coverage")
    source_mode = acquisition.source_metadata.get("acquisition_mode")
    production_source_blockers: list[str] = []
    if source_mode != "pinned-pbf":
        production_source_blockers.append("source is not a pinned PBF")
    if (
        source_timestamp_kind
        not in {
            "osmosis-replication-cutoff",
            "pbf-header-snapshot",
        }
        or normalized_source_timestamp is None
    ):
        production_source_blockers.append(
            "PBF header has no valid UTC snapshot/cutoff timestamp"
        )
    if (
        not isinstance(coverage, dict)
        or coverage.get("covers_requested_bbox") is not True
    ):
        production_source_blockers.append(
            "PBF header bounds do not prove coverage of the acquisition extent"
        )
    if theme is not None:
        acquisition.source_metadata["themed_source_readiness"] = {
            "schema_version": 1,
            "policy_id": theme.source_policy_id,
            "production_source_ready": not production_source_blockers,
            "production_requested": args.production,
            "claim_scope": "selected-family lineage relative to supplied source",
            "normalized_snapshot_timestamp": normalized_source_timestamp,
            "blocking_reasons": production_source_blockers,
        }
    if theme is not None and args.production:
        normalized_source_timestamp = _normalise_production_source_timestamp(
            source_timestamp
        )
        if source_timestamp_kind not in {
            "osmosis-replication-cutoff",
            "pbf-header-snapshot",
        }:
            raise MapPlotterError(
                "A themed production PBF requires a snapshot/cutoff timestamp "
                "in its header; the latest timestamp of selected objects is not "
                "an acquisition cutoff."
            )
        if (
            not isinstance(coverage, dict)
            or coverage.get("covers_requested_bbox") is not True
        ):
            raise MapPlotterError(
                "A themed production PBF must carry a valid header bounding box "
                "that covers the complete padded acquisition extent. Use a "
                "larger dated extract with retained header bounds."
            )
        acquisition.source_metadata["source_timestamp"] = normalized_source_timestamp

    features = (
        acquisition.features
        if acquisition.features is not None
        else extract_features(acquisition.data, {style.id for style in styles})
    )
    if "roads" in families and acquisition.features is None:
        acquisition.source_metadata["highway_coverage"] = highway_coverage(
            acquisition.data
        )
    if not features and not landmark_refs:
        raise MapPlotterError(
            "No drawable features were found for the selected area and layers."
        )
    subject_copy = (
        resolve_subject_copy(theme, catalog_subject, layout)
        if theme is not None and catalog_subject is not None
        else None
    )
    title = (
        subject_copy.title
        if subject_copy is not None
        else args.title
        or resolved_place
        or args.output.stem.replace("_", " ").replace("-", " ").title()
    )
    subtitle = subject_copy.subtitle if subject_copy is not None else args.subtitle
    detail_lines = (
        subject_copy.details if subject_copy is not None else tuple(args.detail)
    )
    if args.preset in POSTER_PRESETS and args.title is None and subject_copy is None:
        title = title.split(",", maxsplit=1)[0]
    if (
        args.preset in POSTER_PRESETS
        and args.poster_layout == "classic"
        and not detail_lines
    ):
        latitude, longitude = layout.bbox.center
        latitude_label = f"{abs(latitude):.4f} {'N' if latitude >= 0 else 'S'}"
        longitude_label = f"{abs(longitude):.4f} {'E' if longitude >= 0 else 'W'}"
        purpose_label = {
            "campus": "UNIVERSITY CAMPUS",
            "student_city": "STUDENT CITY",
            "city_preview": "CITY BASEMAP PREVIEW",
        }.get(
            catalog_subject.map_purpose if catalog_subject is not None else "",
            "CITY CENTRE",
        )
        detail_lines = (
            purpose_label,
            f"{latitude_label} / {longitude_label}",
            *(
                (f"APPROX SCALE 1:{round(layout.scale_denominator):d}",)
                if args.scale_detail
                else ()
            ),
        )

    def render_to(output_path: Path) -> dict[str, Any]:
        return render_svg(
            output_path,
            title=title,
            features=features,
            styles=styles,
            layout=layout,
            acquisition=acquisition,
            simplify_mm=simplify_mm,
            families=families,
            include_frame=args.frame or args.preset in POSTER_PRESETS,
            include_attribution=args.attribution_mode == "embedded",
            include_scale_bar=args.scale_bar,
            external_attribution_placement=args.external_attribution_placement,
            subtitle=subtitle,
            detail_lines=detail_lines,
            road_style=road_style,
            optimise=args.optimise,
            extent_fit=args.extent_fit,
            detail_profile=args.detail_profile,
            physical_audit=args.physical_audit,
            pen_inventory=pen_inventory,
            allowed_nibs_mm=allowed_nibs_mm,
            allow_repeat_passes=args.allow_repeat_passes,
            stock_id=args.stock_id,
            stock_tone=args.stock_tone,
            pen_down_speed=args.pen_down_speed,
            accept_physical_conflicts=args.accept_physical_conflicts,
            require_production_ready=args.production,
            design_contract=design_contract,
            water_fill=args.water_fill,
            landmark_buildings=args.landmark_buildings,
            landmark_refs=landmark_refs,
            poster_layout=args.poster_layout,
            person_name=args.person_name,
            degree=args.degree,
            honours=args.honours,
            years=args.years,
        )

    if args.split_by_pen:
        with tempfile.TemporaryDirectory(prefix="city-map-plotter-") as directory:
            staged_master = Path(directory) / args.output.name
            manifest = render_to(staged_master)
            pen_paths = _pen_output_paths(
                args.output,
                manifest["pen_sequence"],
                output_dir=args.pen_output_dir,
            )
            _preflight_export_paths(
                args.output,
                manifest_path,
                pen_paths=pen_paths,
                protected_paths=protected_sources,
            )
            _publish_staged_svg(staged_master, args.output)
    else:
        manifest = render_to(args.output)
    if args.split_by_pen:
        manifest["pen_files"] = write_pen_svgs(
            args.output,
            manifest,
            output_dir=args.pen_output_dir,
            protected_paths=tuple(protected_sources),
        )
    write_manifest(manifest_path, manifest)

    summary = {
        "svg": str(args.output.resolve()),
        "manifest": str(manifest_path.resolve()),
        "features": len(features),
        "detail_profile": args.detail_profile,
        "pen_profile": pen_inventory.id if pen_inventory is not None else "style",
        "production_ready": manifest["production_readiness"]["production_ready"],
        "output_mode": manifest["production_readiness"]["mode"],
        "production_blockers": manifest["production_readiness"]["blocking_reasons"],
        "paper": f"{layout.page.name} {layout.page.orientation}",
        "approximate_scale": f"1:{layout.scale_denominator:,.0f}",
        "source": (
            "saved file"
            if acquisition.endpoint.startswith("file:")
            else "cache"
            if acquisition.from_cache
            else "download"
        ),
    }
    if catalog_subject is not None:
        summary["catalog_subject"] = catalog_subject.id
        if catalog_subject.is_city_preview_only:
            summary["notice"] = (
                "City basemap preview only; the marathon course is not included. "
                "Import and verify an official route before producing course artwork."
            )
    if design_contract is not None:
        summary["theme"] = design_contract["theme_id"]
        summary["edition_signature_sha256"] = design_contract[
            "edition_signature_sha256"
        ]
    if args.split_by_pen:
        summary["pen_files"] = len(manifest.get("pen_files", []))
    print(json.dumps(summary, indent=2))
    print("\nPen sequence:")
    for step in manifest["pen_sequence"]:
        print(f"  {step['step']}. {step['instruction']}")
    if not manifest["production_readiness"]["production_ready"]:
        print(
            "\nREVIEW OUTPUT ONLY — do not send this file to the plotter until "
            "the production_blockers above are resolved."
        )
    return 0


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print(
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    )
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _run_catalog_export(args: argparse.Namespace, catalog: Catalog) -> int:
    if not isfinite(args.delay_seconds) or args.delay_seconds < 0:
        raise MapPlotterError("--delay-seconds must be a finite non-negative number.")
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise MapPlotterError(
            f"Batch output directory {output_dir} is not a directory."
        )
    collection_ids = (
        [collection.id for collection in catalog.collections]
        if args.all_collections
        else list(args.collection)
    )
    plan = build_batch_plan(
        catalog,
        collection_ids=collection_ids,
        output_dir=output_dir,
        catalog_file=args.catalog_file,
        export_args=args.export_args,
        limit=args.limit,
        title_mode=args.title_mode,
        png_dpi=args.png_dpi if args.png else None,
    )
    report_path = (args.report or default_report_path(plan)).expanduser().resolve()
    if report_path.exists() and not report_path.is_file():
        raise MapPlotterError(f"Batch report path {report_path} is not a file.")

    # Parse every generated invocation before the dry run or the first write.
    # This deliberately delegates current and future fidelity/pen options to
    # the ordinary export parser instead of maintaining a second option model.
    export_parser = _parser()
    parsed_exports: dict[str, argparse.Namespace] = {}
    protected_paths: dict[str, Path] = {}
    for item in plan["items"]:
        parsed = export_parser.parse_args(item["export_argv"])
        if parsed.command != "export":
            raise MapPlotterError("Internal batch plan did not produce an export.")
        parsed_exports[str(item["subject_id"])] = parsed
        for name, value in vars(parsed).items():
            if isinstance(value, Path) and name not in {
                "output",
                "manifest",
                "cache_dir",
                "pen_output_dir",
            }:
                protected_paths[str(value.expanduser().resolve())] = value

    uses_public_service = any(
        parsed.input_pbf is None for parsed in parsed_exports.values()
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    **plan,
                    "dry_run": True,
                    "report": str(report_path),
                    "resume": args.resume,
                    "overwrite": args.overwrite,
                    "keep_going": args.keep_going,
                    "delay_between_items": uses_public_service,
                    "delay_seconds": args.delay_seconds,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.png and shutil.which("inkscape") is None:
        raise MapPlotterError(
            "PNG export requires Inkscape on PATH; install Inkscape or omit --png."
        )

    def render_item(item: dict[str, Any]) -> dict[str, Any] | None:
        parsed = parsed_exports[str(item["subject_id"])]
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            exit_code = _run_export(parsed)
        if exit_code != 0:
            raise MapPlotterError(
                f"Export for {item['subject_id']} returned status {exit_code}."
            )
        if args.png:
            png_record = _rasterize_png(
                Path(str(item["output"])),
                Path(str(item["png"])),
                args.png_dpi,
            )
            manifest_path = Path(str(item["manifest"]))
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MapPlotterError(
                    f"Could not append PNG provenance to {manifest_path}: {exc}"
                ) from exc
            if not isinstance(manifest, dict):
                raise MapPlotterError(
                    f"Plot manifest {manifest_path} must contain a JSON object."
                )
            manifest["raster_exports"] = [png_record]
            write_manifest(manifest_path, manifest)
        summary_text = captured.getvalue().split("\n\nPen sequence:", maxsplit=1)[0]
        try:
            summary = json.loads(summary_text)
        except json.JSONDecodeError:
            return None
        if isinstance(summary, dict) and args.png:
            summary["png"] = str(Path(str(item["png"])).resolve())
            summary["png_dpi"] = args.png_dpi
        return summary if isinstance(summary, dict) else None

    report, result = execute_batch_plan(
        plan,
        report_path=report_path,
        render_item=render_item,
        resume=args.resume,
        overwrite=args.overwrite,
        keep_going=args.keep_going,
        delay_seconds=args.delay_seconds,
        delay_between_items=uses_public_service,
        protected_paths=tuple(protected_paths.values()),
        progress=lambda message: print(message, file=sys.stderr),
    )
    result["collections"] = collection_ids
    result["marathon_city_basemaps"] = plan["marathon_city_basemap_count"]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if report["summary"]["failed"] else 0


def _run_catalog(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog_file)
    if args.catalog_action == "export":
        return _run_catalog_export(args, catalog)
    if args.catalog_action == "collections":
        records = [
            {
                "id": collection.id,
                "title": collection.title,
                "kind": collection.kind,
                "scope": collection.scope,
                "as_of": collection.as_of,
                "count": len(collection.entries),
                "methodology": collection.methodology,
                "source_urls": list(collection.source_urls),
                "audit": collection.audit,
            }
            for collection in catalog.collections
        ]
        if args.json:
            print(
                json.dumps(
                    {
                        "catalog_version": catalog.version,
                        "as_of": catalog.as_of,
                        "collections": records,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            _print_table(
                ("ID", "TYPE", "COUNT", "TITLE"),
                [
                    (
                        collection.id,
                        collection.kind,
                        str(len(collection.entries)),
                        collection.title,
                    )
                    for collection in catalog.collections
                ],
            )
        return 0

    if args.catalog_action == "show":
        record = subject_record(catalog, catalog.subject(args.subject_id))
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0

    selected = select_subjects(
        catalog,
        collection_id=args.collection,
        kind=args.kind,
        country_code=args.country,
    )
    records = []
    for subject, entry in selected:
        record = subject_record(catalog, subject)
        if entry is not None:
            record["selected_entry"] = {
                "position": entry.position,
                **entry.attributes,
            }
        records.append(record)
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        rows = []
        for subject, entry in selected:
            rank = ""
            position = ""
            if entry is not None:
                position = str(entry.position)
                rank_value = entry.attributes.get("rank")
                rank = "" if rank_value is None else str(rank_value)
            rows.append(
                (
                    position,
                    rank,
                    subject.id,
                    subject.kind,
                    subject.location_label,
                    subject.name,
                )
            )
        _print_table(("#", "RANK", "ID", "TYPE", "PLACE", "SUBJECT"), rows)
        print(
            f"\n{len(rows)} subject(s); catalog {catalog.version}, checked {catalog.as_of}."
        )
    return 0


def _run_pens(args: argparse.Namespace) -> int:
    if args.pens_action != "calibration":
        raise MapPlotterError(f"Unknown pens action {args.pens_action!r}.")
    manifest_path = args.manifest or args.output.with_suffix(".pens.json")
    protected = (args.pen_inventory,) if args.pen_inventory is not None else ()
    _preflight_export_paths(
        args.output,
        manifest_path,
        protected_paths=protected,
    )
    inventory = (
        load_pen_inventory(args.pen_inventory)
        if args.pen_inventory is not None
        else resolve_pen_inventory(args.pen_profile)
    )
    if inventory is None:
        raise MapPlotterError("Calibration requires a concrete pen inventory.")
    manifest = write_pen_calibration_svg(
        args.output,
        inventory,
        stock_id=args.stock_id,
        stock_tone=args.stock_tone,
        pen_down_speed=args.pen_down_speed,
    )
    write_manifest(manifest_path, manifest)
    print(
        json.dumps(
            {
                "svg": str(args.output.resolve()),
                "manifest": str(manifest_path.resolve()),
                "pen_profile": inventory.id,
                "stock_id": args.stock_id,
                "stock_tone": args.stock_tone,
                "pen_down_speed": args.pen_down_speed,
                "pens": len(manifest["selection"]["selected_pen_ids"]),
                "excluded_pens": len(manifest["selection"]["excluded_pens"]),
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if arguments and arguments[0] == "export":
            arguments = [
                "export",
                *expand_theme_export_args(arguments[1:]),
            ]
        args = parser.parse_args(arguments)
        if args.command == "export":
            return _run_export(args)
        if args.command == "catalog":
            return _run_catalog(args)
        if args.command == "pens":
            return _run_pens(args)
    except MapPlotterError as exc:
        parser.error(str(exc))
    return 2
