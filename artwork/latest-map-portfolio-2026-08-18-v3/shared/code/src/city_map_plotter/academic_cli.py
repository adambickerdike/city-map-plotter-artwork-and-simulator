"""Command-line entry point for rights-cleared academic artwork inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .academic import academic_presets, build_academic_plate, load_academic_record
from .models import MapPlotterError
from .niche_common import render_plate, write_plate


DEFAULT_OUTPUT_DIR = Path("output/academic-artwork-v1")


def _assert_output_directory(path: Path) -> None:
    if path.is_symlink():
        raise MapPlotterError(
            f"Academic output directory {path} is a symlink; choose a real directory."
        )
    if path.exists() and not path.is_dir():
        raise MapPlotterError(f"Academic output target {path} is not a directory.")
    if path.exists() and any(path.iterdir()):
        raise MapPlotterError(
            f"Academic output directory {path} is not empty; choose a new or empty directory."
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-academic",
        description=(
            "Validate or build rights-cleared academic, scientific, engineering, "
            "thesis, publication, and graduation pen artwork."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    presets = commands.add_parser("presets", help="List academic product presets.")
    presets.add_argument("--json", action="store_true")

    validate = commands.add_parser(
        "validate", help="Validate one input and report any manual-review limitation."
    )
    validate.add_argument("input", type=Path)
    validate.add_argument("--json", action="store_true")

    build = commands.add_parser(
        "build", help="Build one layered master SVG, manifest, preview, and pen jobs."
    )
    build.add_argument("input", type=Path)
    build.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    build.add_argument("--dpi", type=float, default=300.0)
    build.add_argument("--no-png", action="store_true")
    build.add_argument("--no-split-pens", action="store_true")
    return parser


def _presets(args: argparse.Namespace) -> int:
    records = academic_presets()
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    for preset in records:
        print(
            f"{preset['id']:<30} {preset['default_format']:<14} "
            f"{preset['minimum_elements']}-{preset['maximum_elements']} elements  "
            f"{preset['label']}"
        )
    return 0


def _validation_report(path: Path) -> dict[str, Any]:
    loaded = load_academic_record(path)
    record = loaded.record
    buildable = not bool(record["review_required"])
    if buildable:
        render_plate(
            build_academic_plate(loaded),
            generated_at="2000-01-01T00:00:00+00:00",
        )
    return {
        "valid": True,
        "buildable": buildable,
        "id": record["id"],
        "preset": record["preset"],
        "format_id": record["format_id"],
        "rights_status": record["rights_status"],
        "composition_mode": record["composition_mode"],
        "source_count": len(record["sources"]),
        "element_count": len(record["elements"]),
        "loaded_assets": loaded.asset_evidence,
        "limitations": list(record["limitations"]),
    }


def _validate(args: argparse.Namespace) -> int:
    report = _validation_report(args.input)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        status = "BUILDABLE" if report["buildable"] else "MANUAL REVIEW REQUIRED"
        print(f"{report['id']}: {status}")
        for limitation in report["limitations"]:
            print(f"- {limitation}")
    return 0 if report["buildable"] else 3


def _build(args: argparse.Namespace) -> int:
    if not isinstance(args.dpi, (int, float)) or args.dpi <= 0:
        raise MapPlotterError("Academic preview DPI must be greater than zero.")
    _assert_output_directory(args.output_dir)
    loaded = load_academic_record(args.input)
    artwork = build_academic_plate(loaded)
    outputs = write_plate(
        artwork,
        args.output_dir,
        png=not args.no_png,
        png_dpi=float(args.dpi),
        split_pens=not args.no_split_pens,
    )
    print(f"Built {artwork.document_title or artwork.title}")
    print(f"SVG: {outputs['svg']['path']}")
    print(f"Manifest: {outputs['manifest']['path']}")
    if "png" in outputs:
        print(f"Preview: {outputs['png']['path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "presets":
            return _presets(args)
        if args.command == "validate":
            return _validate(args)
        return _build(args)
    except (MapPlotterError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"mapplot-academic: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
