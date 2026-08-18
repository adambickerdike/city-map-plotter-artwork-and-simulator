"""Build pinned, physically scaled place-art plates from GeoJSON requests."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from .models import MapPlotterError
from .niche_common import write_plate
from .place_art import available_place_presets, build_place_artwork_from_file


DEFAULT_OUTPUT_DIR = Path("output/place-art-review")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_paths(value: Any, stage: Path, final: Path) -> Any:
    if isinstance(value, dict):
        return {key: _replace_paths(item, stage, final) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, stage, final) for item in value]
    if isinstance(value, str):
        stage_text = str(stage.resolve())
        if value == stage_text or value.startswith(stage_text + os.sep):
            relative = Path(value).relative_to(stage.resolve())
            return str(final.resolve() / relative)
    return value


def _rewrite_manifest(path: Path, stage: Path, final: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MapPlotterError(
            f"Could not finalize plot manifest {path}: {exc}"
        ) from exc
    manifest = _replace_paths(manifest, stage, final)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def _render(args: argparse.Namespace) -> int:
    if not math.isfinite(args.dpi) or args.dpi <= 0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    output_dir = Path(args.output_dir).absolute()
    if output_dir.exists():
        raise MapPlotterError(
            f"Output directory {output_dir} already exists; choose a new path so "
            "no artwork is overwritten."
        )
    requests = [Path(path).absolute() for path in args.request]
    if len(requests) != len(set(requests)):
        raise MapPlotterError("A place-art request path cannot be repeated.")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent)
    )
    generated_at = args.generated_at or datetime.now(UTC).isoformat()
    artifacts: list[dict[str, Any]] = []
    subject_ids: set[str] = set()
    try:
        for index, request_path in enumerate(requests, start=1):
            artwork = build_place_artwork_from_file(request_path)
            if artwork.subject_id in subject_ids:
                raise MapPlotterError(
                    f"Place-art requests repeat subject ID {artwork.subject_id!r}."
                )
            subject_ids.add(artwork.subject_id)
            print(
                f"[{index:02d}/{len(requests):02d}] {artwork.title}",
                flush=True,
            )
            outputs = write_plate(
                artwork,
                stage,
                png=not args.no_png,
                png_dpi=args.dpi,
                split_pens=not args.no_split_pens,
                generated_at=generated_at,
            )
            manifest_path = Path(str(outputs["manifest"]["path"]))
            manifest = _rewrite_manifest(manifest_path, stage, output_dir)
            artifacts.append(
                {
                    "subject_id": artwork.subject_id,
                    "title": artwork.title,
                    "preset": manifest["rendering"]["place_art_preset_id"],
                    "format_id": artwork.context.format_id,
                    "svg": f"{artwork.artifact_id}.svg",
                    "plot_manifest": f"{artwork.artifact_id}.plot.json",
                    "png": (f"{artwork.artifact_id}.png" if "png" in outputs else None),
                    "physical_pen_steps": manifest["plot_summary"][
                        "physical_pen_steps"
                    ],
                    "field_ink_coverage": manifest["plot_summary"][
                        "field_ink_coverage_upper_bound"
                    ],
                    "source_geometry_sha256": manifest["rendering"][
                        "place_art_geometry"
                    ]["canonical_geojson_sha256"],
                    "svg_sha256": _sha256(stage / f"{artwork.artifact_id}.svg"),
                }
            )
        series = {
            "schema_version": 1,
            "series_id": "place-art-review-v1",
            "generated_at": generated_at,
            "production_ready": False,
            "production_blockers": [
                "built-in physical pens remain nominal and uncalibrated for one stock and speed",
                "each supplied source and depicted property still requires its declared commercial-rights review",
            ],
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        }
        (stage / "place-art-series.json").write_text(
            json.dumps(series, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(stage, output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    print(f"Built {len(artifacts)} place-art plate(s) in {output_dir}.")
    print("REVIEW OUTPUT ONLY — calibrate exact pens and clear rights before plotting.")
    return 0


def _presets(args: argparse.Namespace) -> int:
    values = available_place_presets()
    if args.json:
        print(json.dumps(values, indent=2))
    else:
        for value in values:
            print(f"{value['id']:<31} {value['label']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-place",
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    presets = commands.add_parser("presets", help="List the ten place-art presets.")
    presets.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    render = commands.add_parser(
        "render",
        help="Build one or more pinned place-art requests atomically.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False,
    )
    render.add_argument(
        "--request",
        action="append",
        required=True,
        help="Version-1 place-art request JSON; repeat to build a family.",
    )
    render.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    render.add_argument("--no-png", action="store_true")
    render.add_argument("--no-split-pens", action="store_true")
    render.add_argument("--dpi", type=float, default=180.0)
    render.add_argument(
        "--generated-at",
        help="Fixed ISO timestamp for a repeatable review manifest.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if args.command == "presets":
            return _presets(args)
        if args.command == "render":
            return _render(args)
    except MapPlotterError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
