"""Command-line preparation and export for engineered-object plates."""

from __future__ import annotations

import argparse
import copy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from .models import MapPlotterError
from .niche_common import write_plate
from .technical import (
    CATALOG_PATH,
    PRESETS,
    build_technical_plate,
    load_technical_catalog,
)
from .technical_collections import (
    COLLECTION_IDS,
    FORMAT_IDS,
)
from .technical_assets import import_svg_asset, raster_reconstruction_primitives
from .technical_geometry import load_grayscale_image, reconstruct_raster_reference
from .technical_pdf import (
    ExtractionCrop,
    extraction_geometry_sha256,
    import_pdf_page_asset,
)
from .technical_raster import (
    PixelRect,
    extract_binary_centrelines,
    load_verified_binary_image,
)
from .technical_source_audit import (
    load_source_audit,
    source_audit_summary,
    validate_named_subject_release_bindings,
)


DEFAULT_OUTPUT_DIR = Path("output/technical-objects-v1")


def _retired_collection_error(collection: str) -> MapPlotterError:
    """Explain why the old real-subject collection cannot be rendered.

    The v1 collection expanded dimensions and hand-authored parameters into
    recognisable-looking objects.  That is useful test geometry, but it is not
    source-observed geometry and must never be presented as a technical outline
    of a named vehicle, aircraft, or vessel.
    """

    return MapPlotterError(
        f"The {collection!r} parametric collection is retired: its contours are "
        "project-authored illustrative geometry, not imported technical "
        "outlines. Supply a source-qualified technical catalog with "
        "--catalog-file. Missing views must remain blocked; there is no "
        "procedural fallback. See "
        "docs/technical-objects/SOURCE_GEOMETRY_POLICY_V2.md."
    )


def _select_records(
    records: list[dict[str, Any]], *, build_all: bool, subject_ids: list[str]
) -> list[dict[str, Any]]:
    if build_all:
        return records
    wanted = set(subject_ids)
    selected = [record for record in records if record["id"] in wanted]
    missing = sorted(wanted - {str(record["id"]) for record in selected})
    if missing:
        raise MapPlotterError(
            "Unknown technical object subject(s): " + ", ".join(missing) + "."
        )
    if not selected:
        raise MapPlotterError("Choose --all or at least one --subject ID.")
    return selected


def _assert_safe_target(path: Path) -> None:
    if path.is_symlink():
        raise MapPlotterError(f"Output directory {path} is a symlink.")
    if not path.exists():
        return
    if not path.is_dir():
        raise MapPlotterError(f"Output target {path} is not a directory.")
    if any(path.iterdir()):
        raise MapPlotterError(
            f"Output directory {path} is not empty; choose a new or empty directory."
        )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise MapPlotterError(f"Cannot hash source {path}: {exc}") from exc
    return digest.hexdigest()


def _tool_version(executable: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [executable, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise MapPlotterError(
            f"Cannot run provenance tool {executable}: {exc}"
        ) from exc
    lines = (result.stdout + "\n" + result.stderr).strip().splitlines()
    if not lines:
        raise MapPlotterError(f"Provenance tool {executable} reported no version.")
    return lines[0].strip()


def verify_pdf_embedded_image_provenance(
    parent: Path,
    *,
    page: int,
    image_index: int,
    upstream_sha256: str,
    executable: str,
) -> str:
    version = _tool_version(executable, "-v")
    with tempfile.TemporaryDirectory(prefix="mapplot-parent-image-") as directory:
        prefix = Path(directory) / "page"
        try:
            result = subprocess.run(
                [
                    executable,
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-png",
                    str(parent),
                    str(prefix),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MapPlotterError(
                f"Cannot extract the recorded parent PDF image: {exc}"
            ) from exc
        candidate = Path(f"{prefix}-{image_index:03d}.png")
        if result.returncode != 0 or not candidate.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise MapPlotterError(
                "Parent PDF page/image extraction failed: "
                + (detail or f"no image index {image_index} was emitted")
            )
        actual_sha256 = _sha256(candidate)
        if actual_sha256 != upstream_sha256:
            raise MapPlotterError(
                "Parent PDF embedded image does not match the upstream image: "
                f"expected {upstream_sha256}, got {actual_sha256}."
            )
    return version


def verify_mode1_png_conversion(
    upstream: Path,
    *,
    output_sha256: str,
    expected_size_px: tuple[int, int],
    executable: str,
) -> str:
    version = _tool_version(executable, "-version")
    try:
        identity = subprocess.run(
            [
                executable,
                "identify",
                "-format",
                "%m|%z|%k|%[colorspace]|%w|%h",
                str(upstream),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise MapPlotterError(
            f"Cannot inspect the upstream one-bit image: {exc}"
        ) from exc
    try:
        image_format, depth, colours, colourspace, width, height = identity.split("|")
        inspected_size = (int(width), int(height))
    except ValueError as exc:
        raise MapPlotterError(
            f"Upstream one-bit image identity is malformed: {identity!r}."
        ) from exc
    if (
        image_format != "PNG"
        or depth != "1"
        or int(colours) > 2
        or colourspace != "Gray"
        or inspected_size != expected_size_px
    ):
        raise MapPlotterError(
            "Upstream image must already be an exact one-bit, at-most-two-colour "
            f"grayscale PNG of size {expected_size_px[0]}x{expected_size_px[1]}; "
            f"got {identity!r}."
        )
    with tempfile.TemporaryDirectory(prefix="mapplot-mode1-conversion-") as directory:
        converted = Path(directory) / "source.pbm"
        try:
            result = subprocess.run(
                [executable, str(upstream), "-compress", "none", str(converted)],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MapPlotterError(
                f"Cannot replay the recorded one-bit source conversion: {exc}"
            ) from exc
        if result.returncode != 0 or not converted.is_file():
            detail = (result.stderr or result.stdout).strip()
            raise MapPlotterError(
                "One-bit source conversion replay failed: "
                + (detail or "no PBM was emitted")
            )
        actual_sha256 = _sha256(converted)
        if actual_sha256 != output_sha256:
            raise MapPlotterError(
                "Converted one-bit source does not match --input: "
                f"expected {output_sha256}, got {actual_sha256}."
            )
    return version


def _write_index(
    directory: Path,
    records: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    catalog_path: Path,
    *,
    generated_at: str,
) -> None:
    index = {
        "schema_version": 1,
        "kind": "technical-object-artifact-index",
        "generated_at": generated_at,
        "catalog_path": str(catalog_path),
        "subjects": [
            {
                "id": record["id"],
                "category": record["category"],
                "preset": record["preset"],
                "source_level": record["source_level"],
                "claim_scope": record["claim_scope"],
                "outputs": output,
            }
            for record, output in zip(records, outputs, strict=True)
        ],
    }
    (directory / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _replace_paths(value: Any, staging: Path, target: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                str(target / Path(item).relative_to(staging))
                if key == "path" and isinstance(item, str)
                else _replace_paths(item, staging, target)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_paths(item, staging, target) for item in value]
    return copy.deepcopy(value)


def _render(args: argparse.Namespace) -> int:
    if args.collection and args.catalog_file:
        raise MapPlotterError("Choose --collection or --catalog-file, not both.")
    if args.collection:
        raise _retired_collection_error(str(args.collection))
    else:
        catalog_path = Path(args.catalog_file) if args.catalog_file else None
        records = load_technical_catalog(catalog_path)
    selected = _select_records(
        records,
        build_all=bool(args.all),
        subject_ids=list(args.subject or []),
    )
    candidates: list[dict[str, Any]] = []
    for record in selected:
        candidate = copy.deepcopy(record)
        if args.preset:
            candidate["preset"] = args.preset
        if args.format_id:
            candidate["format_id"] = args.format_id
        if args.density:
            candidate["style"]["density"] = args.density
        candidates.append(candidate)
    release_bindings = validate_named_subject_release_bindings(
        candidates,
        tuple(getattr(args, "release_binding", None) or ()),
    )
    target = _absolute(Path(args.output_dir))
    _assert_safe_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-stage-", dir=target.parent))
    generated_at = args.generated_at or datetime.now(UTC).isoformat()
    try:
        staged_outputs: list[dict[str, Any]] = []
        rendered_records: list[dict[str, Any]] = []
        for candidate in candidates:
            artwork = build_technical_plate(
                candidate,
                release_binding=release_bindings.get(str(candidate["id"])),
            )
            outputs = write_plate(
                artwork,
                staging,
                png=not args.no_png,
                png_dpi=float(args.png_dpi),
                split_pens=not args.no_split_pens,
                generated_at=generated_at,
            )
            staged_outputs.append(outputs)
            rendered_records.append(candidate)
        final_outputs = [
            _replace_paths(output, staging, target) for output in staged_outputs
        ]
        # Rewrite manifests so their output paths refer to the final directory.
        for staged, finalized in zip(staged_outputs, final_outputs, strict=True):
            manifest_path = Path(staged["manifest"]["path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_outputs = copy.deepcopy(finalized)
            manifest_outputs["manifest"].pop("sha256", None)
            manifest["outputs"] = manifest_outputs
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            finalized["manifest"]["sha256"] = _sha256(manifest_path)
        _write_index(
            staging,
            rendered_records,
            final_outputs,
            catalog_path or CATALOG_PATH,
            generated_at=generated_at,
        )
        if target.exists():
            target.rmdir()
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(f"Wrote {len(selected)} technical object plate(s) to {target}")
    return 0


def _list(args: argparse.Namespace) -> int:
    if args.collection and args.catalog_file:
        raise MapPlotterError("Choose --collection or --catalog-file, not both.")
    if args.collection:
        raise _retired_collection_error(str(args.collection))
    records = load_technical_catalog(
        Path(args.catalog_file) if args.catalog_file else None
    )
    for record in records:
        print(
            f"{record['id']}\t{record['category']}\t{record['preset']}\t"
            f"level-{record['source_level']}\t{record['format_id']}"
        )
    return 0


def _audit_sources(args: argparse.Namespace) -> int:
    collections = [args.collection] if args.collection else sorted(COLLECTION_IDS)
    summaries = [
        source_audit_summary(load_source_audit(collection))
        for collection in collections
    ]
    if args.json:
        print(json.dumps({"collections": summaries}, indent=2, sort_keys=True))
    else:
        for summary in summaries:
            counts = summary["status_counts"]
            print(
                f"{summary['collection']}\t"
                f"source={summary['source_covered_view_count']}/"
                f"{summary['required_view_count']} views\t"
                f"validated={summary['qualified_view_count']}/"
                f"{summary['required_view_count']} views\t"
                f"ready={counts['ready']} partial={counts['partial']} "
                f"rights={counts['blocked-rights']} "
                f"geometry={counts['blocked-geometry']} "
                f"validation={counts['blocked-validation']}"
            )
    incomplete = [
        summary["collection"] for summary in summaries if not summary["complete"]
    ]
    if args.require_ready and incomplete:
        raise MapPlotterError(
            "Source-qualified collection is incomplete: " + ", ".join(incomplete) + "."
        )
    return 0


def _write_preparation(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _import_svg(args: argparse.Namespace) -> int:
    source = Path(args.input)
    imported = import_svg_asset(
        source,
        source_ref=args.source_ref,
        view=args.view,
        expected_sha256=args.sha256,
        default_semantic_class=args.semantic_class,
        default_evidence=args.evidence,
    )
    output = Path(args.output)
    _write_preparation(
        output,
        {
            "schema_version": 1,
            "kind": "technical-source-preparation",
            "source": {
                "path": str(_absolute(source)),
                "sha256": imported.sha256,
                "view_box": list(imported.view_box),
            },
            "view": {
                "id": args.view,
                "type": args.view_type,
                "label": args.label,
                "unit": args.unit,
                "axis_direction": args.axis_direction,
                "scale_status": args.scale_status,
                "source_refs": [args.source_ref],
                "primitives": [primitive.record() for primitive in imported.primitives],
                "dimensions": [],
                "callouts": [],
            },
        },
    )
    print(f"Prepared {len(imported.primitives)} vector primitives in {output}")
    return 0


def _quad(value: str) -> list[list[float]]:
    points: list[list[float]] = []
    try:
        for raw_point in value.split(";"):
            x, y = raw_point.split(",", 1)
            points.append([float(x), float(y)])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "quad must be x,y;x,y;x,y;x,y in corner order"
        ) from exc
    if len(points) != 4:
        raise argparse.ArgumentTypeError("quad must contain exactly four points")
    return points


def _crop(value: str) -> ExtractionCrop:
    try:
        x, y, width, height = (float(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("crop must be x,y,width,height") from exc
    return ExtractionCrop(x, y, width, height)


def _element_range(value: str) -> tuple[int, int]:
    try:
        start_text, end_text = value.split("-", 1)
        start, end = int(start_text), int(end_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("element range must be START-END") from exc
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError(
            "element range must contain positive ascending indices"
        )
    return start, end


def _source_path(value: str) -> tuple[int, int]:
    try:
        element_text, subpath_text = value.split(":", 1)
        element, subpath = int(element_text), int(subpath_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("source path must be ELEMENT:SUBPATH") from exc
    if element < 1 or subpath < 1:
        raise argparse.ArgumentTypeError(
            "source path must contain positive element and subpath indices"
        )
    return element, subpath


def _stroke_semantic(value: str) -> tuple[float, str]:
    try:
        width_text, semantic = value.split("=", 1)
        width = float(width_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "stroke semantic must be WIDTH=SEMANTIC_CLASS"
        ) from exc
    if width <= 0 or not semantic:
        raise argparse.ArgumentTypeError(
            "stroke semantic needs a positive width and non-empty semantic class"
        )
    return width, semantic


def _pixel_rect(value: str) -> PixelRect:
    try:
        x, y, width, height = (int(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "pixel rectangle must be integer x,y,width,height"
        ) from exc
    return PixelRect(x, y, width, height)


def _pixel_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pixel size must be WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("pixel size must be positive")
    return width, height


def _import_pdf(args: argparse.Namespace) -> int:
    source = Path(args.input)
    excluded_elements = frozenset(
        index
        for start, end in (args.exclude_element_range or ())
        for index in range(start, end + 1)
    )
    imported = import_pdf_page_asset(
        source,
        page=int(args.page),
        crop=args.crop,
        source_ref=args.source_ref,
        view=args.view,
        expected_sha256=args.sha256,
        include_fill_boundaries=bool(args.include_fill_boundaries),
        default_evidence=args.evidence,
        inkscape_executable=args.inkscape,
        exclusion_zones=tuple(args.exclude or ()),
        excluded_element_indices=excluded_elements,
        leader_minimum_length=args.leader_minimum_length,
        leader_stroke_width=args.leader_stroke_width,
        principal_source_path=args.principal_source_path,
        semantic_by_stroke_width=tuple(args.stroke_semantic or ()),
        stitch_exact_subpaths=bool(args.stitch_exact_subpaths),
    )
    primitive_records: list[dict[str, Any]] = []
    for primitive in imported.primitives:
        record = primitive.record()
        record["detail_priority"] = args.detail_priority
        primitive_records.append(record)
    output = Path(args.output)
    _write_preparation(
        output,
        {
            "schema_version": 1,
            "kind": "technical-source-preparation",
            "source": {
                "path": str(_absolute(source)),
                "sha256": imported.pdf_sha256,
                "page": imported.page,
                "crop_normalized_svg": imported.crop.record(),
                "converter": imported.converter,
                "converter_version": imported.converter_version,
                "normalized_svg_sha256": imported.normalized_svg_sha256,
                "vector_only": True,
                "text_policy": "delete-all-not-outlined",
            },
            "extraction": {
                "geometry_sha256": extraction_geometry_sha256(imported.primitives),
                "paint_policy": (
                    "source-strokes-and-solid-fill-boundaries"
                    if args.include_fill_boundaries
                    else "source-strokes-only"
                ),
                "crop_policy": "whole-path-containment-no-clipping",
                "exclusion_zones_normalized_svg": [
                    region.record() for region in (args.exclude or ())
                ],
                "excluded_normalized_svg_element_indices": sorted(excluded_elements),
                "leader_filter": (
                    {
                        "kind": "open-line-only-source-path",
                        "maximum_segments": 3,
                        "minimum_source_length": args.leader_minimum_length,
                        "source_stroke_width": args.leader_stroke_width,
                    }
                    if args.leader_minimum_length is not None
                    else None
                ),
                "principal_source_path": (
                    list(args.principal_source_path)
                    if args.principal_source_path is not None
                    else None
                ),
                "semantic_by_source_stroke_width": [
                    {"source_stroke_width": width, "semantic_class": semantic}
                    for width, semantic in (args.stroke_semantic or ())
                ],
                "stitch_exact_subpaths_within_source_element": bool(
                    args.stitch_exact_subpaths
                ),
                "statistics": imported.stats.record(),
            },
            "view": {
                "id": args.view,
                "type": args.view_type,
                "label": args.label,
                "unit": args.unit,
                "axis_direction": "y-down",
                "scale_status": args.scale_status,
                "source_refs": [args.source_ref],
                "primitives": primitive_records,
                "dimensions": [],
                "callouts": [],
            },
        },
    )
    print(
        f"Prepared {len(imported.primitives)} native PDF vector primitives in {output}"
    )
    return 0


def _import_raster(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if args.pdfimages != "pdfimages" or args.magick != "magick":
        raise MapPlotterError(
            "Strict raster provenance uses the fixed pdfimages and magick executables."
        )
    parent_values = (
        args.parent_input,
        args.parent_sha256,
        args.parent_page,
        args.parent_image_index,
    )
    if any(value is not None for value in parent_values) and not all(
        value is not None for value in parent_values
    ):
        raise MapPlotterError(
            "--parent-input, --parent-sha256, --parent-page and "
            "--parent-image-index must be supplied together."
        )
    if args.parent_page is not None and (
        args.parent_page < 1 or args.parent_image_index < 0
    ):
        raise MapPlotterError(
            "Parent page must be positive and image index non-negative."
        )
    if args.parent_sha256 is not None and (
        len(args.parent_sha256) != 64
        or any(character not in "0123456789abcdef" for character in args.parent_sha256)
    ):
        raise MapPlotterError("--parent-sha256 must be one lowercase SHA-256 digest.")
    parent_path = Path(args.parent_input) if args.parent_input else None
    if parent_path is not None:
        actual_parent_sha256 = _sha256(parent_path)
        if actual_parent_sha256 != args.parent_sha256:
            raise MapPlotterError(
                "Parent source SHA-256 changed: expected "
                f"{args.parent_sha256}, got {actual_parent_sha256}."
            )
    upstream_values = (
        args.upstream_image,
        args.upstream_image_sha256,
        args.source_conversion,
    )
    if any(upstream_values) and not all(upstream_values):
        raise MapPlotterError(
            "--upstream-image, --upstream-image-sha256 and --source-conversion must be supplied together."
        )
    if args.upstream_image_sha256 is not None and (
        len(args.upstream_image_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in args.upstream_image_sha256
        )
    ):
        raise MapPlotterError(
            "--upstream-image-sha256 must be one lowercase SHA-256 digest."
        )
    upstream_path = Path(args.upstream_image) if args.upstream_image else None
    if upstream_path is not None:
        actual_upstream_sha256 = _sha256(upstream_path)
        if actual_upstream_sha256 != args.upstream_image_sha256:
            raise MapPlotterError(
                "Upstream image SHA-256 changed: expected "
                f"{args.upstream_image_sha256}, got {actual_upstream_sha256}."
            )
    if parent_path is not None and upstream_path is None:
        raise MapPlotterError(
            "A parent PDF provenance chain also requires the extracted --upstream-image."
        )
    source_conversion_version: str | None = None
    parent_image_extractor_version: str | None = None
    if upstream_path is not None:
        source_conversion_version = verify_mode1_png_conversion(
            upstream_path,
            output_sha256=args.sha256,
            expected_size_px=args.expected_size,
            executable=args.magick,
        )
    if parent_path is not None:
        parent_image_extractor_version = verify_pdf_embedded_image_provenance(
            parent_path,
            page=int(args.parent_page),
            image_index=int(args.parent_image_index),
            upstream_sha256=str(args.upstream_image_sha256),
            executable=args.pdfimages,
        )
    useful_bbox = args.useful_subject_bbox
    if useful_bbox.right > args.crop.width or useful_bbox.bottom > args.crop.height:
        raise MapPlotterError(
            "--useful-subject-bbox must stay inside the crop-local pixel bounds."
        )
    if not 0 < float(args.maximum_half_pixel_mm) <= 0.125:
        raise MapPlotterError(
            "--maximum-half-pixel-mm must be greater than zero and no more than 0.125."
        )
    exclusion_mask = None
    exclusion_mask_source: dict[str, Any] | None = None
    if bool(args.exclusion_mask) != bool(args.exclusion_mask_sha256):
        raise MapPlotterError(
            "--exclusion-mask and --exclusion-mask-sha256 must be supplied together."
        )
    if args.exclusion_mask:
        mask_path = Path(args.exclusion_mask)
        exclusion_mask, mask_sha256 = load_verified_binary_image(
            mask_path,
            expected_sha256=args.exclusion_mask_sha256,
            expected_size_px=(args.crop.width, args.crop.height),
        )
        exclusion_mask_source = {
            "path": str(_absolute(mask_path)),
            "sha256": mask_sha256,
            "size_px": [args.crop.width, args.crop.height],
            "true_pixels_are_excluded": True,
        }
    imported = extract_binary_centrelines(
        source,
        expected_sha256=args.sha256,
        expected_size_px=args.expected_size,
        crop=args.crop,
        ink_is_one=bool(args.ink_is_one),
        exclusion_rectangles=tuple(args.exclude or ()),
        exclusion_mask=exclusion_mask,
        simplification_tolerance_px=float(args.simplification_tolerance),
    )
    if useful_bbox != imported.retained_ink_bbox:
        raise MapPlotterError(
            "--useful-subject-bbox must exactly match the retained source-ink "
            f"bounds {imported.retained_ink_bbox.record()}."
        )
    principal_index = int(args.principal_path_index) - 1
    if not 0 <= principal_index < len(imported.paths):
        raise MapPlotterError(
            f"--principal-path-index must be between 1 and {len(imported.paths)}."
        )
    primitive_records: list[dict[str, Any]] = []
    for index, path in enumerate(imported.paths):
        points = [[float(x), float(y)] for x, y in path.points]
        if path.closed:
            points.append(points[0])
        is_principal = index == principal_index
        primitive_records.append(
            {
                "id": f"{args.view}-raster-path-{index + 1:05d}",
                "component_id": args.component_id,
                "semantic_class": (
                    "principal_silhouette" if is_principal else args.semantic_class
                ),
                "source_refs": [args.source_ref],
                "evidence": args.evidence,
                "claim_status": "source-visible-binary-centreline",
                "feature_kind": "binary-centreline",
                "points": points,
                "detail_priority": "identity" if is_principal else args.detail_priority,
            }
        )
    source_record: dict[str, Any] = {
        "path": str(_absolute(source)),
        "sha256": imported.source_sha256,
        "size_px": list(imported.source_size_px),
        "crop_source_px": imported.crop.record(),
        "binary_ink_is_one": imported.ink_is_one,
    }
    if args.parent_sha256:
        assert parent_path is not None
        source_record["parent_path"] = str(_absolute(parent_path))
        source_record["parent_sha256"] = args.parent_sha256
        source_record["parent_page"] = args.parent_page
        source_record["parent_image_index"] = args.parent_image_index
        source_record["parent_image_extractor"] = args.pdfimages
        source_record["parent_image_extractor_version"] = parent_image_extractor_version
    if args.upstream_image_sha256:
        assert upstream_path is not None
        source_record["upstream_image_path"] = str(_absolute(upstream_path))
        source_record["upstream_image_sha256"] = args.upstream_image_sha256
        source_record["upstream_image_size_px"] = list(args.expected_size)
        source_record["upstream_image_mode"] = "png-depth-1-gray-at-most-2-colours"
        source_record["source_conversion"] = args.source_conversion
        source_record["source_conversion_tool"] = args.magick
        source_record["source_conversion_tool_version"] = source_conversion_version
    provenance_payload = {
        "schema_version": 1,
        "source_sha256": imported.source_sha256,
        "parent_sha256": source_record.get("parent_sha256"),
        "parent_page": source_record.get("parent_page"),
        "parent_image_index": source_record.get("parent_image_index"),
        "upstream_image_sha256": source_record.get("upstream_image_sha256"),
        "upstream_image_size_px": source_record.get("upstream_image_size_px"),
        "upstream_image_mode": source_record.get("upstream_image_mode"),
        "source_conversion": source_record.get("source_conversion"),
        "source_conversion_tool": source_record.get("source_conversion_tool"),
        "source_conversion_tool_version": source_record.get(
            "source_conversion_tool_version"
        ),
        "parent_image_extractor": source_record.get("parent_image_extractor"),
        "parent_image_extractor_version": source_record.get(
            "parent_image_extractor_version"
        ),
        "extraction_sha256": imported.extraction_sha256,
    }
    provenance_sha256 = hashlib.sha256(
        json.dumps(
            provenance_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    output = Path(args.output)
    _write_preparation(
        output,
        {
            "schema_version": 1,
            "kind": "technical-source-preparation",
            "source": source_record,
            "extraction": {
                "mode": "one-bit-centreline",
                "method": "pinned-one-bit-zhang-suen-centreline-v1",
                "geometry_sha256": imported.geometry_sha256,
                "extraction_sha256": imported.extraction_sha256,
                "provenance_sha256": provenance_sha256,
                "retained_ink_sha256": imported.retained_ink_sha256,
                "retained_ink_bbox_crop_px": imported.retained_ink_bbox.record(),
                "skeleton_sha256": imported.skeleton_sha256,
                "exclusion_rectangles_source_px": [
                    rectangle.record() for rectangle in imported.exclusion_rectangles
                ],
                "exclusion_mask": exclusion_mask_source,
                "source_ink_replay_passed": (
                    imported.stats.replay_off_ink_sample_count == 0
                ),
                "primitive_adapter": {
                    "id_pattern": f"{args.view}-raster-path-%05d",
                    "principal_path_index": int(args.principal_path_index),
                    "component_id": args.component_id,
                    "nonprincipal_semantic_class": args.semantic_class,
                    "evidence": args.evidence,
                    "nonprincipal_detail_priority": args.detail_priority,
                    "claim_status": "source-visible-binary-centreline",
                    "feature_kind": "binary-centreline",
                },
                "statistics": imported.stats.record(),
            },
            "view": {
                "id": args.view,
                "type": args.view_type,
                "label": args.label,
                "unit": "source-pixel",
                "axis_direction": "y-down",
                "scale_status": args.scale_status,
                "source_refs": [args.source_ref],
                "raster_sampling": {
                    "source_unit": "source-pixel",
                    "source_crop_size_px": [args.crop.width, args.crop.height],
                    "useful_subject_bbox": [
                        useful_bbox.x,
                        useful_bbox.y,
                        useful_bbox.width,
                        useful_bbox.height,
                    ],
                    "maximum_projected_half_pixel_mm": float(
                        args.maximum_half_pixel_mm
                    ),
                    "method": "one-bit-centreline",
                },
                "primitives": primitive_records,
                "dimensions": [],
                "callouts": [],
            },
        },
    )
    print(
        f"Prepared {len(imported.paths)} replay-audited binary centrelines in {output}"
    )
    return 0


def _import_photo(args: argparse.Namespace) -> int:
    source = Path(args.input)
    image = load_grayscale_image(source)
    reconstruction = reconstruct_raster_reference(
        image,
        category=args.category,
        perspective_source_points=args.quad,
        auto_perspective=bool(args.auto_perspective),
        foreground_difference=int(args.foreground_difference),
        edge_threshold=float(args.edge_threshold),
        minimum_component_pixels=int(args.minimum_component_pixels),
        smoothing_iterations=int(args.smoothing_iterations),
    )
    if reconstruction.quality_status != "usable-visible-portrait":
        raise MapPlotterError(
            "INSUFFICIENT_REFERENCE: photo did not pass reconstruction quality; "
            + " ".join(reconstruction.limitations)
        )
    primitives = raster_reconstruction_primitives(
        reconstruction,
        source_ref=args.source_ref,
        view=args.view,
    )
    output = Path(args.output)
    _write_preparation(
        output,
        {
            "schema_version": 1,
            "kind": "technical-source-preparation",
            "source": {
                "path": str(source),
                "width_px": reconstruction.width_px,
                "height_px": reconstruction.height_px,
            },
            "quality": {
                "status": reconstruction.quality_status,
                "foreground_fraction": reconstruction.foreground_fraction,
                "raw_edge_pixel_count": reconstruction.raw_edge_pixel_count,
                "retained_contour_count": reconstruction.retained_contour_count,
                "discarded_component_count": reconstruction.discarded_component_count,
                "perspective_corrected": reconstruction.perspective_corrected,
                "limitations": list(reconstruction.limitations),
                "raw_edge_map_emitted": False,
            },
            "view": {
                "id": args.view,
                "type": args.view_type,
                "label": args.label,
                "unit": "source-pixel",
                "axis_direction": "y-down",
                "scale_status": "visible-view-only",
                "source_refs": [args.source_ref],
                "primitives": primitives,
                "dimensions": [],
                "callouts": [],
            },
        },
    )
    print(f"Prepared {len(primitives)} cleaned visible contours in {output}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-objects",
        description="Prepare and render source-qualified engineered-object pen plates.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="List records in a technical catalog.")
    listing.add_argument("--catalog-file")
    listing.add_argument("--collection", choices=sorted(COLLECTION_IDS))
    listing.set_defaults(handler=_list)

    source_audit = subparsers.add_parser(
        "audit-sources",
        help="Verify named-collection source coverage, reuse rights and bundled hashes.",
    )
    source_audit.add_argument("--collection", choices=sorted(COLLECTION_IDS))
    source_audit.add_argument("--json", action="store_true")
    source_audit.add_argument(
        "--require-ready",
        action="store_true",
        help="Fail unless every requested view of every selected subject is qualified.",
    )
    source_audit.set_defaults(handler=_audit_sources)

    render = subparsers.add_parser(
        "render", help="Render one or more technical records."
    )
    render.add_argument("--catalog-file")
    render.add_argument("--collection", choices=sorted(COLLECTION_IDS))
    render.add_argument(
        "--release-binding",
        action="append",
        help=(
            "Canonical checked-in v2 release binding for one selected named "
            "collection subject; repeat for multi-subject renders."
        ),
    )
    selection = render.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--subject", action="append")
    render.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    render.add_argument("--preset", choices=sorted(PRESETS))
    render.add_argument(
        "--format-id",
        choices=FORMAT_IDS,
    )
    render.add_argument("--density", choices=("sparse", "medium", "rich"))
    render.add_argument("--no-png", action="store_true")
    render.add_argument("--png-dpi", type=float, default=180.0)
    render.add_argument("--no-split-pens", action="store_true")
    render.add_argument(
        "--generated-at",
        help="Fixed ISO-8601 artifact timestamp for reproducible review builds.",
    )
    render.set_defaults(handler=_render)

    svg = subparsers.add_parser(
        "import-svg",
        help="Prepare a local unfilled SVG as canonical vector primitives.",
    )
    svg.add_argument("--input", required=True)
    svg.add_argument("--output", required=True)
    svg.add_argument("--source-ref", required=True)
    svg.add_argument("--sha256")
    svg.add_argument("--view", required=True)
    svg.add_argument("--view-type", default="side")
    svg.add_argument("--label", default="SUPPLIED VECTOR VIEW")
    svg.add_argument("--unit", default="source-unit")
    svg.add_argument("--axis-direction", choices=("y-up", "y-down"), default="y-down")
    svg.add_argument(
        "--scale-status",
        choices=(
            "verified-common-scale",
            "dimension-calibrated",
            "not-to-scale",
            "visible-view-only",
        ),
        default="not-to-scale",
    )
    svg.add_argument("--semantic-class", default="panel_seam_lines")
    svg.add_argument("--evidence", default="supplied-visible")
    svg.set_defaults(handler=_import_svg)

    pdf = subparsers.add_parser(
        "import-pdf",
        help="Prepare one hash-pinned native-vector technical PDF crop.",
    )
    pdf.add_argument("--input", required=True)
    pdf.add_argument("--output", required=True)
    pdf.add_argument("--source-ref", required=True)
    pdf.add_argument("--sha256", required=True)
    pdf.add_argument("--page", required=True, type=int)
    pdf.add_argument("--crop", required=True, type=_crop)
    pdf.add_argument(
        "--exclude",
        action="append",
        type=_crop,
        help="Reject paths wholly contained in this source-coordinate rectangle.",
    )
    pdf.add_argument(
        "--exclude-element-range",
        action="append",
        type=_element_range,
        help="Reject normalized SVG source elements in the inclusive START-END range.",
    )
    pdf.add_argument(
        "--leader-minimum-length",
        type=float,
        help="Reject long open all-line paths with at most three segments.",
    )
    pdf.add_argument(
        "--leader-stroke-width",
        type=float,
        help="Apply the leader filter only to this source stroke width.",
    )
    pdf.add_argument(
        "--principal-source-path",
        type=_source_path,
        help="Select the accepted ELEMENT:SUBPATH as the principal structural path.",
    )
    pdf.add_argument(
        "--stroke-semantic",
        action="append",
        type=_stroke_semantic,
        help="Map an exact source stroke width to a semantic class as WIDTH=CLASS.",
    )
    pdf.add_argument(
        "--stitch-exact-subpaths",
        action="store_true",
        help="Join non-branching exact endpoints within one PDF source element.",
    )
    pdf.add_argument("--view", required=True)
    pdf.add_argument("--view-type", default="plan")
    pdf.add_argument("--label", default="SOURCE-DERIVED PLAN VIEW")
    pdf.add_argument("--unit", default="source-unit")
    pdf.add_argument(
        "--scale-status",
        choices=("dimension-calibrated", "not-to-scale"),
        default="not-to-scale",
    )
    pdf.add_argument(
        "--evidence",
        choices=("repository-verified", "supplied-visible"),
        default="repository-verified",
    )
    pdf.add_argument(
        "--detail-priority",
        choices=("identity", "normal", "optional"),
        default="normal",
    )
    pdf.add_argument("--include-fill-boundaries", action="store_true")
    pdf.add_argument("--inkscape", default="inkscape")
    pdf.set_defaults(handler=_import_pdf)

    raster = subparsers.add_parser(
        "import-raster",
        help="Prepare a hash-pinned one-bit technical drawing as source centrelines.",
    )
    raster.add_argument("--input", required=True)
    raster.add_argument("--output", required=True)
    raster.add_argument("--source-ref", required=True)
    raster.add_argument("--sha256", required=True)
    raster.add_argument("--expected-size", required=True, type=_pixel_size)
    raster.add_argument("--crop", required=True, type=_pixel_rect)
    polarity = raster.add_mutually_exclusive_group(required=True)
    polarity.add_argument("--ink-is-one", action="store_true")
    polarity.add_argument("--ink-is-zero", action="store_true")
    raster.add_argument(
        "--exclude",
        action="append",
        type=_pixel_rect,
        help="Source-coordinate rectangle whose pixels are explicitly excluded.",
    )
    raster.add_argument("--exclusion-mask")
    raster.add_argument("--exclusion-mask-sha256")
    raster.add_argument("--parent-input")
    raster.add_argument("--parent-sha256")
    raster.add_argument("--parent-page", type=int)
    raster.add_argument("--parent-image-index", type=int)
    raster.add_argument("--upstream-image")
    raster.add_argument("--upstream-image-sha256")
    raster.add_argument(
        "--source-conversion",
        choices=("imagemagick-mode1-png-to-p1-v1",),
    )
    raster.add_argument("--pdfimages", default="pdfimages")
    raster.add_argument("--magick", default="magick")
    raster.add_argument("--simplification-tolerance", type=float, default=0.25)
    raster.add_argument("--principal-path-index", type=int, required=True)
    raster.add_argument("--useful-subject-bbox", type=_pixel_rect, required=True)
    raster.add_argument("--maximum-half-pixel-mm", type=float, default=0.125)
    raster.add_argument("--view", required=True)
    raster.add_argument("--view-type", default="exploded")
    raster.add_argument("--label", default="SOURCE-DERIVED RASTER CENTRELINES")
    raster.add_argument("--component-id", default="movement")
    raster.add_argument("--semantic-class", default="mechanical_detail")
    raster.add_argument(
        "--scale-status",
        choices=("not-to-scale", "visible-view-only"),
        default="not-to-scale",
    )
    raster.add_argument(
        "--evidence",
        choices=("repository-verified", "supplied-visible"),
        default="repository-verified",
    )
    raster.add_argument(
        "--detail-priority",
        choices=("identity", "normal", "optional"),
        default="normal",
    )
    raster.set_defaults(handler=_import_raster)

    photo = subparsers.add_parser(
        "import-photo",
        help="Prepare cleaned visible-view candidates from a local photograph.",
    )
    photo.add_argument("--input", required=True)
    photo.add_argument("--output", required=True)
    photo.add_argument("--source-ref", required=True)
    photo.add_argument("--category", required=True)
    photo.add_argument("--view", required=True)
    photo.add_argument("--view-type", default="three-quarter")
    photo.add_argument("--label", default="SUPPLIED VISIBLE VIEW")
    photo.add_argument("--quad", type=_quad)
    photo.add_argument("--auto-perspective", action="store_true")
    photo.add_argument("--foreground-difference", type=int, default=18)
    photo.add_argument("--edge-threshold", type=float, default=80.0)
    photo.add_argument("--minimum-component-pixels", type=int, default=8)
    photo.add_argument("--smoothing-iterations", type=int, default=1)
    photo.set_defaults(handler=_import_photo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (MapPlotterError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
