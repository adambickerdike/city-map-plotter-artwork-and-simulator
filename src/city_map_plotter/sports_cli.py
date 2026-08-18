"""Command-line entry point for source-labelled sports artwork."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, NoReturn

from .models import MapPlotterError
from .niche_common import write_plate
from .sports import SPORTS_PRESETS, build_sports_plate, list_sports_presets
from .sports_route import load_route_file


FORMATS = (
    "a5-portrait",
    "a5-landscape",
    "a4-portrait",
    "a4-landscape",
    "a3-portrait",
    "a3-landscape",
)


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number {value!r} is forbidden")


def _load_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MapPlotterError(f"Could not read sports record {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MapPlotterError("A sports record must be one JSON object.")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Could not hash route file {path}: {exc}") from exc
    return digest.hexdigest()


def _write(args: argparse.Namespace, record: dict[str, Any]) -> int:
    if not math.isfinite(args.dpi) or args.dpi <= 0.0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    artwork = build_sports_plate(
        record,
        None if args.format == "record" else args.format,
        preset_id=args.preset,
    )
    outputs = write_plate(
        artwork,
        args.output_dir.resolve(),
        png=not args.no_png,
        png_dpi=args.dpi,
        split_pens=not args.no_split_pens,
    )
    print(f"Built {artwork.artifact_id}: {outputs['svg']['path']}")
    print(f"Plot manifest: {outputs['manifest']['path']}")
    return 0


def _build_json(args: argparse.Namespace) -> int:
    return _write(args, _load_record(args.record.resolve()))


def _build_route(args: argparse.Namespace) -> int:
    if not args.confirm_rights:
        raise MapPlotterError(
            "Route-file artwork requires --confirm-rights to confirm permission "
            "to reproduce the supplied track and performance data."
        )
    if not args.privacy_reviewed:
        raise MapPlotterError(
            "Route-file artwork requires --privacy-reviewed after checking "
            "start/end, home-location, timestamp, and health-data exposure."
        )
    path = args.route.resolve()
    route = load_route_file(
        path,
        source_ref=args.source_ref,
        discipline=args.discipline,
    )
    route["direction_marks"] = args.direction_marks
    if args.render_style is not None:
        route["render_style"] = args.render_style
    source = {
        "id": args.source_ref,
        "publisher": args.publisher,
        "license": args.license,
        "attribution": args.attribution,
        "use": "user-supplied route geometry and embedded channels",
        "sha256": _sha256(path),
        "input_file": path.name,
    }
    event: dict[str, Any] = {
        "name": args.title,
        "location": args.location,
        "discipline": args.discipline,
    }
    if args.date:
        event["date"] = args.date
    if args.official_distance is not None:
        event["official_distance"] = {
            "value": args.official_distance,
            "unit": args.official_distance_unit,
            "source_ref": args.source_ref,
            "status": "caller-supplied-authoritative-metadata",
        }
    participant: dict[str, Any] = {}
    if args.participant:
        participant["name"] = args.participant
    if args.finish_time:
        participant["finish_time"] = args.finish_time
    if args.bib_number:
        participant["bib_number"] = args.bib_number
    record: dict[str, Any] = {
        "schema_version": 1,
        "id": args.id,
        "preset": args.preset or "route-hero",
        "title": args.title,
        "subtitle": args.subtitle or None,
        "event": event,
        "participant": participant,
        "sources": [source],
        "route": route,
        "rights_status": "user-rights-unverified",
        "evidence_status": "user-supplied-route-file",
        "notes": [
            "Local route input; privacy and commercial rights remain the caller's responsibility."
        ],
    }
    if record["subtitle"] is None:
        record.pop("subtitle")
    if not participant:
        record.pop("participant")
    return _write(args, record)


def _add_output_arguments(
    parser: argparse.ArgumentParser,
    *,
    allow_record_format: bool,
) -> None:
    formats = ("record", *FORMATS) if allow_record_format else FORMATS
    parser.add_argument("--preset", choices=tuple(SPORTS_PRESETS))
    parser.add_argument(
        "--format",
        choices=formats,
        default="record" if allow_record_format else "a4-portrait",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output/sports"))
    parser.add_argument("--dpi", type=float, default=180.0)
    parser.add_argument("--no-png", action="store_true")
    parser.add_argument("--no-split-pens", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-sports",
        description=(
            "Build source-labelled route, race, circuit, river, sailing, and "
            "stadium pen artwork on the canonical plate system."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    presets = commands.add_parser("presets", help="List the fourteen sports concepts.")
    presets.add_argument("--json", action="store_true")

    build = commands.add_parser("build", help="Build one validated JSON sports record.")
    build.add_argument("record", type=Path)
    _add_output_arguments(build, allow_record_format=True)

    route = commands.add_parser(
        "route",
        help="Build from GPX, TCX, KML, GeoJSON, or FIT-derived JSON.",
    )
    route.add_argument("route", type=Path)
    route.add_argument("--id", required=True)
    route.add_argument("--title", required=True)
    route.add_argument("--subtitle")
    route.add_argument("--location", default="SUPPLIED ROUTE")
    route.add_argument("--date")
    route.add_argument("--discipline", default="run")
    route.add_argument("--participant")
    route.add_argument("--finish-time")
    route.add_argument("--bib-number")
    route.add_argument("--official-distance", type=float)
    route.add_argument("--official-distance-unit", default="km")
    route.add_argument("--source-ref", default="user-route")
    route.add_argument("--publisher", default="User-supplied local route")
    route.add_argument("--license", default="user-rights-declaration-required")
    route.add_argument("--attribution", default="USER-SUPPLIED ROUTE")
    route.add_argument(
        "--direction-marks",
        choices=("none", "arrows", "progressive", "kilometre-ticks", "checkpoints"),
        default="arrows",
    )
    route.add_argument(
        "--render-style",
        choices=(
            "single-line",
            "parallel-dual",
            "ribbon-edges",
            "hatched-corridor",
            "variable-density",
            "highlighted-line",
        ),
    )
    route.add_argument("--confirm-rights", action="store_true")
    route.add_argument("--privacy-reviewed", action="store_true")
    _add_output_arguments(route, allow_record_format=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "presets":
            values = list_sports_presets()
            if args.json:
                print(json.dumps(values, indent=2))
            else:
                for preset in values:
                    print(f"{preset['letter']}  {preset['id']:<24} {preset['label']}")
            return 0
        if args.command == "build":
            return _build_json(args)
        return _build_route(args)
    except (MapPlotterError, OSError, ValueError) as exc:
        print(f"mapplot-sports: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

