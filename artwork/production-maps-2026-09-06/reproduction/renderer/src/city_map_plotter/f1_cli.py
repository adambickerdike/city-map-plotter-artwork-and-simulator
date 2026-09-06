"""CLI for the frozen, review-only 2026 circuit atlas domain.

The project entry-point table is intentionally owned elsewhere.  Until that
shared packaging file is updated, this module is directly executable with::

    python -m city_map_plotter.f1_cli list
    python -m city_map_plotter.f1_cli build EVENT_ID --format a4-landscape
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any

from .f1_circuits import (
    CATALOG_PATH,
    CONTEXT_MODES,
    FORMAT_IDS,
    RENDERING_PRESET,
    build_f1_plate,
    list_f1_events,
    load_f1_catalog,
)
from .models import MapPlotterError
from .niche_common import PlateArtwork, render_plate, write_plate


DEFAULT_OUTPUT_DIR = Path("output") / "f1-circuits-2026"
FULL_RELEASE_EVENT_COUNT = 23


def _event(catalog: dict[str, Any], event_id: str) -> dict[str, Any]:
    matches = [event for event in catalog["events"] if event["id"] == event_id]
    if len(matches) != 1:
        known = ", ".join(str(event["id"]) for event in catalog["events"])
        raise MapPlotterError(
            f"Unknown circuit event {event_id!r}. Catalog events: {known}."
        )
    return matches[0]


def _preflight_one(
    *,
    event: dict[str, Any],
    catalog: dict[str, Any],
    format_id: str,
    context_mode: str,
    dpi: float,
) -> PlateArtwork:
    if not math.isfinite(dpi) or dpi <= 0.0:
        raise MapPlotterError("--dpi must be a positive finite number.")
    return build_f1_plate(
        event,
        format_id,
        catalog=catalog,
        context_mode=None if context_mode == "auto" else context_mode,
    )


def _write_preflighted(
    *,
    artwork: PlateArtwork,
    event_id: str,
    format_id: str,
    output_dir: Path,
    dpi: float,
    png: bool,
    split_pens: bool,
    generated_at: str | None,
) -> dict[str, Any]:
    outputs = write_plate(
        artwork,
        output_dir.resolve(),
        png=png,
        png_dpi=dpi,
        split_pens=split_pens,
        generated_at=generated_at,
    )
    print(f"Built {artwork.artifact_id}: {outputs['svg']['path']}")
    print(f"Plot manifest: {outputs['manifest']['path']}")
    return {
        "event_id": event_id,
        "artifact_id": artwork.artifact_id,
        "format_id": format_id,
        "outputs": outputs,
    }


def _list(args: argparse.Namespace) -> int:
    rows = list_f1_events(args.catalog.resolve())
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for row in rows:
        order = (
            "--" if row["calendar_order"] is None else f"{row['calendar_order']:02d}"
        )
        readiness = "geometry-ready" if row["renderable"] else "no-render-geometry"
        print(
            f"{order}  {row['id']:<36} {row['circuit_name']} "
            f"[{row['calendar_status']}; {readiness}]"
        )
    return 0


def _validate(args: argparse.Namespace) -> int:
    try:
        catalog = load_f1_catalog(args.catalog.resolve())
    except (MapPlotterError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "structural_pass": False,
                    "renderable_event_count": 0,
                    "release_matrix_ready": False,
                    "full_series_ready": False,
                    "release_status": "review-only",
                    "failures": [str(exc)],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 2
    event_count = len(catalog["events"])
    renderable = sum(
        isinstance(event["circuit"]["geometry"].get("model"), dict)
        for event in catalog["events"]
    )
    release_matrix_ready = event_count > 0 and renderable == event_count
    full_series_ready = (
        release_matrix_ready and event_count == FULL_RELEASE_EVENT_COUNT
    )
    structural_pass = True
    passed = structural_pass and (
        release_matrix_ready if args.require_all_renderable else True
    )
    result = {
        "passed": passed,
        "structural_pass": structural_pass,
        "schema_version": catalog["schema_version"],
        "catalog_id": catalog["catalog_id"],
        "season": catalog["season"],
        "event_count": event_count,
        "renderable_event_count": renderable,
        "release_matrix_ready": release_matrix_ready,
        "full_series_ready": full_series_ready,
        "required_full_release_event_count": FULL_RELEASE_EVENT_COUNT,
        "required_full_release_artifact_count": (
            FULL_RELEASE_EVENT_COUNT * len(FORMAT_IDS)
        ),
        "excluded_calendar_event_count": len(catalog["excluded_calendar_events"]),
        "rendering_preset": RENDERING_PRESET,
        "formats": list(FORMAT_IDS),
        "release_status": "review-only",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if passed else 1


def _build(args: argparse.Namespace) -> int:
    catalog = load_f1_catalog(args.catalog.resolve())
    event = _event(catalog, args.event_id)
    artwork = _preflight_one(
        event=event,
        catalog=catalog,
        format_id=args.format,
        context_mode=args.context_mode,
        dpi=args.dpi,
    )
    _write_preflighted(
        artwork=artwork,
        event_id=str(event["id"]),
        format_id=args.format,
        output_dir=args.output_dir,
        dpi=args.dpi,
        png=not args.no_png,
        split_pens=not args.no_split_pens,
        generated_at=args.generated_at,
    )
    return 0


def _batch(args: argparse.Namespace) -> int:
    catalog = load_f1_catalog(args.catalog.resolve())
    if args.all:
        events = list(catalog["events"])
    else:
        if len(args.event) != len(set(args.event)):
            raise MapPlotterError("--event contains duplicate event IDs.")
        events = [_event(catalog, event_id) for event_id in args.event]
    formats = args.format or ["a4-landscape"]
    if len(formats) != len(set(formats)):
        raise MapPlotterError("--format contains duplicate format IDs.")

    # Build the complete selection in memory before opening the output
    # directory.  A missing model, layout failure, invalid DPI, or colliding
    # artifact identity therefore leaves no partial review batch behind.
    preflighted: list[tuple[dict[str, Any], str, PlateArtwork]] = []
    for event in events:
        for format_id in formats:
            artwork = _preflight_one(
                event=event,
                catalog=catalog,
                format_id=format_id,
                context_mode=args.context_mode,
                dpi=args.dpi,
            )
            render_plate(artwork, generated_at=args.generated_at)
            preflighted.append((event, format_id, artwork))
    artifact_ids = [artwork.artifact_id for _event, _format, artwork in preflighted]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise MapPlotterError("Batch selection produces duplicate artifact IDs.")

    outputs: list[dict[str, Any]] = []
    for event, format_id, artwork in preflighted:
        outputs.append(
            _write_preflighted(
                artwork=artwork,
                event_id=str(event["id"]),
                format_id=format_id,
                output_dir=args.output_dir,
                dpi=args.dpi,
                png=not args.no_png,
                split_pens=not args.no_split_pens,
                generated_at=args.generated_at,
            )
        )
    print(
        json.dumps(
            {
                "built": len(outputs),
                "event_count": len(events),
                "formats": formats,
                "artifact_ids": [output["artifact_id"] for output in outputs],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _add_catalog(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--catalog",
        type=Path,
        default=CATALOG_PATH,
        help="Frozen schema-version-1 circuit catalog.",
    )


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--context-mode",
        choices=("auto", *CONTEXT_MODES),
        default="auto",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dpi", type=float, default=254.0)
    parser.add_argument("--no-png", action="store_true")
    parser.add_argument("--no-split-pens", action="store_true")
    parser.add_argument(
        "--generated-at",
        help="Optional fixed ISO timestamp for a deterministic review rebuild.",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-f1",
        description=(
            "Build source-qualified, north-up circuit atlas plates from the "
            "frozen 2026 catalog. Outputs are always review-only."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_command = commands.add_parser("list", help="List the frozen event ledger.")
    _add_catalog(list_command)
    list_command.add_argument("--json", action="store_true")

    validate = commands.add_parser(
        "validate", help="Validate catalog metadata and normalized geometry."
    )
    _add_catalog(validate)
    validate.add_argument(
        "--require-all-renderable",
        action="store_true",
        help=(
            "Exit 1 unless every catalog event has a normalized geometry model. "
            "Structural catalog errors exit 2."
        ),
    )

    build = commands.add_parser("build", help="Build one event in one exact format.")
    build.add_argument("event_id")
    _add_catalog(build)
    build.add_argument("--format", choices=FORMAT_IDS, default="a4-landscape")
    _add_output(build)

    batch = commands.add_parser(
        "batch",
        help=(
            "Build selected review products after an all-in-memory preflight. "
            "Use tools/build_f1_circuit_series.py for the canonical staged, "
            "atomic full release."
        ),
    )
    _add_catalog(batch)
    selection = batch.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--event", action="append", default=[])
    batch.add_argument(
        "--format",
        action="append",
        choices=FORMAT_IDS,
        help="Repeat to build multiple identities; default a4-landscape.",
    )
    _add_output(batch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            return _list(args)
        if args.command == "validate":
            return _validate(args)
        if args.command == "build":
            return _build(args)
        return _batch(args)
    except (MapPlotterError, OSError, ValueError) as exc:
        print(f"mapplot-f1: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DEFAULT_OUTPUT_DIR", "main"]
