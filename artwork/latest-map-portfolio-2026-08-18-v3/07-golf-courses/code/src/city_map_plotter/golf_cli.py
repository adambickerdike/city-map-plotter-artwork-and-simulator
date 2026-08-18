"""Batch builder for the Twenty-Five Icons of Golf source-map series."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from typing import Any

from .golf import (
    CATALOG_ID,
    FORMAT_ID,
    GREEN_FILL_INSET_MM,
    GREEN_ROUTE_TEXTURE_CLEARANCE_MM,
    build_golf_plate,
    load_golf_catalog,
)
from .models import MapPlotterError
from .niche_common import write_plate


EXPECTED_CATALOG_COUNT = 25
SERIES_TITLE = "TWENTY-FIVE ICONS OF GOLF"
RELEASE_ID = "golf-course-series-v4"
RENDERING_PRESET = "golf-clarity-course-a3-v4"
DEFAULT_OUTPUT_DIR = Path("output") / RELEASE_ID
SERIES_FILE = "golf-course-series.json"
COURSE_BOUNDARY_RENDERING = "raw-root-boundary-omitted-selection-mask-only"
PLAYING_ENVELOPE_RENDERING = (
    "grey-0.40-derived-from-source-hole-routes-and-nearby-playing-surfaces"
    "-illustrative-not-property-or-official-boundary"
)
FAIRWAY_RENDERING = "green-0.25-source-outline-only"
GREEN_AND_TEE_RENDERING = (
    "green-0.40-source-outlines-with-green-only-green-0.25-fine-line-fill"
    "-tees-outline-only"
)
WATER_RENDERING = (
    "blue-0.40-area-outlines-with-blue-0.25-closed-dot-symbols-for-every"
    "-visible-area-linear-and-physically-narrow-water-source"
)
WATER_DOT_ROLES = frozenset(
    {
        "water-area-stipple-dot",
        "water-linear-stipple-dot",
        "water-narrow-boundary-stipple-dot",
        "water-narrow-source-stipple-dot",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select(
    records: list[dict[str, Any]],
    *,
    build_all: bool,
    subject_ids: list[str],
) -> list[dict[str, Any]]:
    if build_all:
        return records
    wanted = set(subject_ids)
    selected = [record for record in records if record["id"] in wanted]
    missing = sorted(wanted - {record["id"] for record in selected})
    if missing:
        raise MapPlotterError("Unknown golf course(s): " + ", ".join(missing) + ".")
    if not selected:
        raise MapPlotterError("Choose --all or at least one --course ID.")
    return selected


def _replace_paths(value: Any, stage: Path, final: Path) -> Any:
    if isinstance(value, dict):
        return {key: _replace_paths(item, stage, final) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, stage, final) for item in value]
    if isinstance(value, str):
        stage_text = str(stage.resolve())
        if value == stage_text or value.startswith(stage_text + os.sep):
            relative = Path(value).relative_to(stage.resolve())
            return str((final.resolve() / relative))
    return value


def _rewrite_plot_manifest(path: Path, stage: Path, final: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest = _replace_paths(manifest, stage, final)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def _assert_manifest(manifest: dict[str, Any], subject_id: str) -> None:
    rendering = manifest.get("rendering") or {}
    summary = manifest.get("plot_summary") or {}
    sequence = manifest.get("pen_sequence") or []
    layers = manifest.get("layers") or []
    logical_layers = {
        str(logical_id)
        for layer in layers
        for logical_id in (layer.get("logical_layers") or [])
    }
    logical_pen_ids = {
        str(logical_id): str(layer.get("pen_id"))
        for layer in layers
        for logical_id in (layer.get("logical_layers") or [])
    }
    if rendering.get("preset") != RENDERING_PRESET:
        raise MapPlotterError(
            f"{subject_id}: manifest does not use the binding v4 golf preset."
        )
    if rendering.get("course_hole_count") != 18:
        raise MapPlotterError(
            f"{subject_id}: manifest does not preserve all 18 source holes."
        )
    if rendering.get("unmapped_features_invented") is not False:
        raise MapPlotterError(
            f"{subject_id}: manifest does not fail closed on invented features."
        )
    if rendering.get("course_boundary_emitted") is not False or (
        rendering.get("course_boundary_rendering") != COURSE_BOUNDARY_RENDERING
    ):
        raise MapPlotterError(
            f"{subject_id}: the raw root boundary is not certified as omitted."
        )
    if "course_boundary" in logical_layers:
        raise MapPlotterError(
            f"{subject_id}: a forbidden visible course-boundary layer was emitted."
        )
    if (
        rendering.get("playing_envelope_emitted") is not True
        or rendering.get("playing_envelope_rendering") != PLAYING_ENVELOPE_RENDERING
        or logical_pen_ids.get("playing_envelope") != "grey-0-4"
    ):
        raise MapPlotterError(
            f"{subject_id}: the illustrative grey playing envelope is not certified."
        )
    if rendering.get("fairway_rendering") != FAIRWAY_RENDERING:
        raise MapPlotterError(
            f"{subject_id}: fairway outline-only rendering is not certified."
        )
    if logical_pen_ids.get("fairways") != "green-0-25":
        raise MapPlotterError(
            f"{subject_id}: fairway source outlines do not use Green 0.25."
        )
    if rendering.get("green_and_tee_rendering") != GREEN_AND_TEE_RENDERING:
        raise MapPlotterError(
            f"{subject_id}: green-only fill and tee outline rendering is not certified."
        )
    if logical_pen_ids.get("greens_and_tees") != "green-0-4":
        raise MapPlotterError(
            f"{subject_id}: green and tee source outlines do not use Green 0.40."
        )
    green_coverage = rendering.get("green_fill_coverage") or {}
    visible_greens = green_coverage.get("visible_source_count")
    filled_greens = green_coverage.get("filled_source_count")
    unfillable_greens = green_coverage.get("physically_unfillable_source_refs")
    if (
        green_coverage.get("fill_inset_mm") != GREEN_FILL_INSET_MM
        or green_coverage.get("fill_pen_nib_mm") != 0.25
        or green_coverage.get("gold_route_clearance_mm")
        != GREEN_ROUTE_TEXTURE_CLEARANCE_MM
        or not isinstance(visible_greens, int)
        or isinstance(visible_greens, bool)
        or visible_greens < 0
        or not isinstance(filled_greens, int)
        or isinstance(filled_greens, bool)
        or filled_greens < 0
        or not isinstance(unfillable_greens, list)
        or unfillable_greens != []
        or filled_greens != visible_greens
        or green_coverage.get("uncovered_fillable_source_refs") != []
    ):
        raise MapPlotterError(
            f"{subject_id}: every visible green is not certified as filled."
        )
    if rendering.get("water_rendering") != WATER_RENDERING:
        raise MapPlotterError(
            f"{subject_id}: complete closed-dot water rendering is not certified."
        )
    water_coverage = rendering.get("water_source_dot_coverage") or {}
    visible_water_sources = water_coverage.get("visible_source_count")
    represented_water_sources = water_coverage.get("represented_source_count")
    if (
        not isinstance(visible_water_sources, int)
        or isinstance(visible_water_sources, bool)
        or visible_water_sources < 0
        or not isinstance(represented_water_sources, int)
        or isinstance(represented_water_sources, bool)
        or represented_water_sources != visible_water_sources
        or water_coverage.get("uncovered_source_refs") != []
    ):
        raise MapPlotterError(
            f"{subject_id}: every visible water source is not certified by dots."
        )
    if visible_water_sources and logical_pen_ids.get("water_stipple") != "blue-0-25":
        raise MapPlotterError(f"{subject_id}: water dot symbols do not use Blue 0.25.")
    final_water_roles = water_coverage.get("dot_role_counts_final")
    if (
        not isinstance(final_water_roles, dict)
        or any(role not in WATER_DOT_ROLES for role in final_water_roles)
        or any(
            not isinstance(count, int) or isinstance(count, bool) or count <= 0
            for count in final_water_roles.values()
        )
        or (visible_water_sources > 0 and not final_water_roles)
    ):
        raise MapPlotterError(
            f"{subject_id}: water dot roles do not match the v4 physical contract."
        )
    if rendering.get("label_feature_overlap_mm") != 0.0:
        raise MapPlotterError(
            f"{subject_id}: a hole number overlaps mapped course ink."
        )
    masking = rendering.get("label_masking") or {}
    if masking.get("records_preserved_whole") != 0:
        raise MapPlotterError(
            f"{subject_id}: label clearance could not preserve a source stroke."
        )
    utilisation = rendering.get("fitted_geometry_working_rect_utilisation") or {}
    if not isinstance(utilisation.get("maximum"), (int, float)) or (
        utilisation["maximum"] < 0.97
    ):
        raise MapPlotterError(
            f"{subject_id}: fitted course geometry does not use the A3 field."
        )
    coverage = summary.get("field_ink_coverage_upper_bound")
    if not isinstance(coverage, (int, float)) or coverage > 0.28:
        raise MapPlotterError(
            f"{subject_id}: field ink coverage exceeds the 28% budget."
        )
    travel = summary.get("travel_ratio")
    if not isinstance(travel, (int, float)) or travel >= 1.0:
        raise MapPlotterError(
            f"{subject_id}: document travel ratio must remain below 1.0."
        )
    pen_ids = [step.get("pen_id") for step in sequence]
    if not pen_ids or len(pen_ids) != len(set(pen_ids)):
        raise MapPlotterError(
            f"{subject_id}: physical pen sequence repeats a pen load."
        )
    if "black-0-6" in pen_ids:
        raise MapPlotterError(
            f"{subject_id}: the retired black-0-6 pen remains in the sequence."
        )
    if (
        rendering.get("scale_bar") is not True
        or rendering.get("north_mark") is not True
    ):
        raise MapPlotterError(f"{subject_id}: map reference furniture is incomplete.")


def _contact_sheet(stage: Path, pngs: list[Path]) -> dict[str, Any] | None:
    executable = shutil.which("montage")
    if executable is None or not pngs:
        return None
    output = stage / "golf-course-contact-sheet.png"
    result = subprocess.run(
        [
            executable,
            *[str(path) for path in pngs],
            "-thumbnail",
            "520x",
            "-tile",
            "5x5",
            "-geometry",
            "+18+18",
            "-background",
            "white",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MapPlotterError(f"Contact-sheet generation failed: {detail}")
    return {"path": output.name, "sha256": _sha256(output)}


def _write_checksums(directory: Path) -> None:
    files = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS.sha256"
    )
    lines = [
        f"{_sha256(path)}  {path.relative_to(directory).as_posix()}" for path in files
    ]
    (directory / "CHECKSUMS.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _build(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).absolute()
    if output_dir.exists():
        raise MapPlotterError(
            f"Output directory {output_dir} already exists; choose a new path so no artifacts are overwritten."
        )
    records = load_golf_catalog(Path(args.catalog) if args.catalog else None)
    selected = _select(records, build_all=args.all, subject_ids=args.course or [])
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".golf-stage-", dir=output_dir.parent))
    generated_at = args.generated_at or datetime.now(UTC).isoformat()
    artifacts: list[dict[str, Any]] = []
    pngs: list[Path] = []
    try:
        for index, record in enumerate(selected, start=1):
            print(f"[{index:02d}/{len(selected):02d}] {record['title']}", flush=True)
            artwork = build_golf_plate(record)
            outputs = write_plate(
                artwork,
                stage,
                png=not args.no_png,
                png_dpi=args.dpi,
                split_pens=not args.no_split_pens,
                generated_at=generated_at,
            )
            manifest_path = Path(str(outputs["manifest"]["path"]))
            manifest = _rewrite_plot_manifest(manifest_path, stage, output_dir)
            _assert_manifest(manifest, str(record["id"]))
            if "png" in outputs:
                pngs.append(Path(str(outputs["png"]["path"])))
            artifacts.append(
                {
                    "subject_id": record["id"],
                    "title": record["title"],
                    "svg": f"{record['id']}.svg",
                    "plot_manifest": f"{record['id']}.plot.json",
                    "png": f"{record['id']}.png" if "png" in outputs else None,
                    "format_id": FORMAT_ID,
                    "rendering_preset": RENDERING_PRESET,
                    "scale_denominator": manifest["rendering"][
                        "plan_scale_denominator"
                    ],
                    "course_page_rotation_deg": manifest["rendering"][
                        "course_page_rotation_deg"
                    ],
                    "fitted_geometry_utilisation": manifest["rendering"][
                        "fitted_geometry_working_rect_utilisation"
                    ],
                    "physical_pen_steps": manifest["plot_summary"][
                        "physical_pen_steps"
                    ],
                    "travel_ratio": manifest["plot_summary"]["travel_ratio"],
                    "field_ink_coverage": manifest["plot_summary"][
                        "field_ink_coverage_upper_bound"
                    ],
                    "source_geometry_sha256": manifest["rendering"][
                        "catalog_geometry_sha256"
                    ],
                    "svg_sha256": _sha256(stage / f"{record['id']}.svg"),
                    "plot_manifest_sha256": _sha256(manifest_path),
                }
            )
        contact = _contact_sheet(stage, pngs)
        source_contract = selected[0]["source_contract"] if selected else {}
        series = {
            "schema_version": 1,
            "series_id": CATALOG_ID,
            "release_id": RELEASE_ID,
            "rendering_preset": RENDERING_PRESET,
            "title": SERIES_TITLE,
            "generated_at": generated_at,
            "mode": "review-only-nominal-unmeasured-pens",
            "production_ready": False,
            "production_blockers": [
                "exact pens, stock, and machine speed have not been physically calibrated",
                "commercial rights and non-endorsement review remains required",
            ],
            "format_id": FORMAT_ID,
            "catalog_count": len(records),
            "artifact_count": len(artifacts),
            "source_contract": source_contract,
            "artifacts": artifacts,
            "contact_sheet": contact,
        }
        (stage / SERIES_FILE).write_text(
            json.dumps(series, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _write_checksums(stage)
        os.replace(stage, output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    print(f"Built {len(artifacts)} source-faithful course plates in {output_dir}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mapplot-golf", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build one or all golf-course plates.")
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--all",
        action="store_true",
        help=f"Build the complete {EXPECTED_CATALOG_COUNT}-course collection.",
    )
    selection.add_argument(
        "--course", action="append", help="Course ID; repeat to build several."
    )
    build.add_argument("--catalog", help="Strict alternate catalog wrapper.")
    build.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    build.add_argument("--dpi", type=float, default=180.0)
    build.add_argument("--no-png", action="store_true")
    build.add_argument("--no-split-pens", action="store_true")
    build.add_argument(
        "--generated-at", help="Fixed ISO timestamp for reproducible review builds."
    )
    build.set_defaults(handler=_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dpi <= 0:
        parser.error("--dpi must be positive")
    try:
        return int(args.handler(args))
    except (MapPlotterError, OSError, ValueError) as exc:
        print(f"mapplot-golf: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
