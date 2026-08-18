"""CLI for the frozen Nürburgring and Le Mans circuit studies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import MapPlotterError
from .motorsport_circuits import (
    CATALOG_PATH,
    FORMAT_IDS,
    build_motorsport_plate,
    list_motorsport_circuits,
    load_motorsport_catalog,
)
from .niche_common import write_plate


DEFAULT_OUTPUT_DIR = Path("output") / "motorsport-circuit-studies-v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-circuits",
        description="Build source-qualified, review-only endurance circuit studies.",
        allow_abbrev=False,
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--json", action="store_true")
    build = commands.add_parser("build")
    build.add_argument("circuit_id")
    build.add_argument("--format", choices=FORMAT_IDS, default="a4-landscape")
    build.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    build.add_argument("--dpi", type=float, default=254.0)
    build.add_argument("--no-png", action="store_true")
    build.add_argument("--no-split-pens", action="store_true")
    build.add_argument("--generated-at")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog = load_motorsport_catalog(args.catalog.resolve())
        if args.command == "list":
            rows = list_motorsport_circuits(catalog)
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                for row in rows:
                    print(f"{row['id']:<44} {row['published_length_m'] / 1000:.3f} km")
            return 0
        artwork = build_motorsport_plate(
            args.circuit_id,
            args.format,
            catalog=catalog,
        )
        outputs = write_plate(
            artwork,
            args.output_dir.resolve(),
            png=not args.no_png,
            png_dpi=args.dpi,
            split_pens=not args.no_split_pens,
            generated_at=args.generated_at,
        )
        print(f"Built {artwork.artifact_id}: {outputs['svg']['path']}")
        return 0
    except (MapPlotterError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
