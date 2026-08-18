"""Command-line entry point for source-qualified transit network plates."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from . import __version__
from .models import MapPlotterError
from .osm import DEFAULT_OVERPASS_URL, default_cache_dir, user_agent_from
from .transit import (
    apply_pen_map,
    catalog_network,
    load_transit_catalog,
    load_transit_network,
)
from .transit_operator_overview import (
    DEFAULT_OVERVIEW_RETRIEVED_AT,
    DEFAULT_OVERVIEW_SNAPSHOT_DATE,
    compile_operator_overview_network,
)
from .transit_operator_snapshot import (
    audit_targeted_operator_geometry,
    extract_targeted_operator_geometry,
)
from .transit_context import (
    ContextSnapshotProvenance,
    DEFAULT_CONTEXT_PROFILE,
    attach_context,
    attach_overpass_file,
    attach_pbf_file,
    attach_pbf_files,
    fetch_transit_context,
)
from .transit_national_rail import (
    apply_schedule_transactions,
    audit_msn_stations_against_naptan,
    load_national_rail_source_pack,
    parse_national_rail_source_pack,
    select_effective_operator_schedules,
)
from .transit_rail_graph import load_osm_rail_graph
from .transit_rail_svg import write_zoomstack_rail_plate
from .transit_source import acquire_osm_transit_contract
from .transit_tfgm import acquire_tfgm_transit_contract
from .transit_svg import write_transit_plate
from .transit_wtt import DEFAULT_OPERATOR_CODES, parse_wtt_archive
from .transit_zoomstack import (
    DEFAULT_GB_BOUNDS,
    DEFAULT_NATIONAL_CONTEXT_ZOOM,
    load_zoomstack_physical_rail,
)


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO date (YYYY-MM-DD)") from exc


def _iso_datetime(value: str) -> str:
    """Return one canonical, timezone-aware ISO-8601 timestamp."""

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected a timezone-aware ISO-8601 timestamp"
        ) from exc
    if parsed.utcoffset() is None or parsed.isoformat() != value:
        raise argparse.ArgumentTypeError(
            "expected a canonical timezone-aware ISO-8601 timestamp "
            "(for example 2026-08-07T12:00:00+00:00)"
        )
    return value


def _print(value: Any, *, pretty: bool = True) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=True,
        )
    )


def _write_atomic_json(path: Path, value: Any) -> None:
    """Stream and replace one requested JSON output after it is complete."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.replace(path)
    except OSError as exc:
        raise MapPlotterError(f"Could not write transit output {path}: {exc}") from exc


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapPlotterError(f"Could not hash transit output {path}: {exc}") from exc
    return digest.hexdigest()


def _default_overview_audit_path(contract_path: Path) -> Path:
    suffix = contract_path.suffix or ".json"
    stem = contract_path.stem if contract_path.suffix else contract_path.name
    return contract_path.with_name(f"{stem}.audit{suffix}")


def _catalog_summary(record: dict[str, Any]) -> dict[str, Any]:
    acquisition = record.get("acquisition", {})
    return {
        "id": record["id"],
        "name": record["name"],
        "kind": record["kind"],
        "scope": record["scope"],
        "format_id": record["format_id"],
        "line_count": len(record.get("lines", [])),
        "acquisition_mode": acquisition.get("mode", "unspecified"),
        "release_gate": acquisition.get("release_gate", "blocked"),
        "release_gate_reason": acquisition.get("release_gate_reason"),
    }


def _network_summary(path: Path) -> dict[str, Any]:
    network = load_transit_network(path)
    return {
        "path": str(path.resolve()),
        "id": network.id,
        "name": network.name,
        "kind": network.kind,
        "scope": network.scope,
        "format_id": network.format_id,
        "snapshot": network.snapshot,
        "validity_status": network.validity_status,
        "geometry_mode": network.geometry_mode,
        "source_count": len(network.sources),
        "line_count": len(network.lines),
        "node_count": len(network.nodes),
        "station_count": sum(node.is_station for node in network.nodes),
        "edge_count": len(network.edges),
        "service_pattern_count": len(network.service_patterns),
        "context_feature_count": len(network.context),
        "declared_omission_count": len(network.omissions),
        "contract_sha256": network.contract_sha256,
    }


def _pen_template(path: Path) -> dict[str, Any]:
    network = load_transit_network(path)
    return {
        "schema_version": 1,
        "network_id": network.id,
        "instructions": (
            "Replace each line mapping only after measuring that physical pen "
            "on the exact paper, speed, pressure, and machine."
        ),
        "lines": {line.id: line.pen.as_dict() for line in network.lines},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapplot-transit",
        description=(
            "Compile and render geographic rail, metro, subway, and tram "
            "network pen plates from pinned, source-qualified contracts."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser(
        "list", help="List audited network presets and source gates."
    )
    list_parser.add_argument("--json", action="store_true")

    inspect_parser = commands.add_parser(
        "inspect", help="Validate and summarize a frozen contract."
    )
    inspect_parser.add_argument("contract", type=_path)

    catalog_parser = commands.add_parser(
        "catalog", help="Show one catalog record with provenance."
    )
    catalog_parser.add_argument("network_id")

    acquire_parser = commands.add_parser(
        "acquire",
        help="Explicitly snapshot and compile an enabled catalog network.",
    )
    acquire_parser.add_argument("network_id")
    acquire_parser.add_argument("--output", type=_path, required=True)
    acquire_parser.add_argument("--cache-dir", type=_path, default=default_cache_dir())
    acquire_parser.add_argument("--user-agent")

    national_parser = commands.add_parser(
        "national-audit",
        help=(
            "Parse and audit a hash-pinned National Rail timetable/station "
            "source pack without inventing geographic alignment."
        ),
    )
    national_parser.add_argument("network_id")
    national_parser.add_argument("--source-pack", type=_path, required=True)
    national_parser.add_argument(
        "--service-date",
        type=_iso_date,
        help="Override the catalog's frozen service date (YYYY-MM-DD).",
    )

    wtt_parser = commands.add_parser(
        "wtt-audit",
        help=(
            "Audit the public, hash-pinned Network Rail WTT XLSX archive for "
            "GR/GW/SN/NT without creating geographic routes."
        ),
    )
    wtt_parser.add_argument("--archive", type=_path, required=True)
    wtt_parser.add_argument("--sha256", required=True)
    wtt_parser.add_argument(
        "--operator-code",
        action="append",
        choices=tuple(sorted(DEFAULT_OPERATOR_CODES)),
        help="Repeat to select operators; defaults to GR, GW, NT, and SN.",
    )

    graph_parser = commands.add_parser(
        "rail-graph-audit",
        help=(
            "Audit a hash-pinned OSM PBF as an exact-node physical rail graph; "
            "this does not claim operator usage."
        ),
    )
    graph_parser.add_argument("--pbf", type=_path, required=True)
    graph_parser.add_argument("--sha256", required=True)
    graph_parser.add_argument(
        "--bounds",
        type=float,
        nargs=4,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        default=DEFAULT_GB_BOUNDS,
        help="Required covered WGS84 extent; defaults to the Great Britain plate.",
    )

    overview_parser = commands.add_parser(
        "operator-overview",
        help=(
            "Compile the full versioned Great Britain passenger-operator OSM "
            "review proof with one 0.4 mm parent line per represented product."
        ),
    )
    overview_parser.add_argument("--pbf", type=_path, required=True)
    overview_parser.add_argument(
        "--sha256",
        required=True,
        help="Lower-case SHA-256 of the exact Great Britain OSM PBF bytes.",
    )
    overview_parser.add_argument("--output", type=_path, required=True)
    overview_parser.add_argument(
        "--audit-output",
        type=_path,
        help="Evidence JSON path; defaults beside --output as *.audit.json.",
    )
    overview_parser.add_argument(
        "--snapshot-date",
        type=_iso_date,
        default=date.fromisoformat(DEFAULT_OVERVIEW_SNAPSHOT_DATE),
        help="Dated source snapshot represented by the contract (YYYY-MM-DD).",
    )
    overview_parser.add_argument(
        "--retrieved-at",
        type=_iso_date,
        default=date.fromisoformat(DEFAULT_OVERVIEW_RETRIEVED_AT),
        help="Actual source retrieval date recorded in provenance (YYYY-MM-DD).",
    )

    physical_rail = commands.add_parser(
        "physical-rail",
        help=(
            "Render the hash-pinned full-GB physical railway layer as an A3 "
            "house-style pen plate, without making operator/service claims."
        ),
    )
    physical_rail.add_argument("--mbtiles", type=_path, required=True)
    physical_rail.add_argument(
        "--sha256",
        required=True,
        help="Lower-case SHA-256 of the exact OS Open Zoomstack MBTiles bytes.",
    )
    physical_rail.add_argument("--output-dir", type=_path, required=True)
    physical_rail.add_argument("--zoom", type=int, default=10)
    physical_rail.add_argument(
        "--context-zoom",
        type=int,
        default=DEFAULT_NATIONAL_CONTEXT_ZOOM,
        help=(
            "Zoomstack zoom for the default quiet national coast/water/major-road/"
            "boundary/city context (default: 6)."
        ),
    )
    physical_rail.add_argument("--simplify-mm", type=float, default=0.04)
    physical_rail.add_argument("--no-png", action="store_true")
    physical_rail.add_argument("--png-dpi", type=float, default=180.0)
    physical_rail.add_argument("--no-split-pens", action="store_true")

    context_parser = commands.add_parser(
        "context",
        help="Attach a pinned, scale-aware basemap snapshot to a route contract.",
    )
    context_parser.add_argument("contract", type=_path)
    context_parser.add_argument("--output", type=_path, required=True)
    source_group = context_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--overpass-file", type=_path)
    source_group.add_argument(
        "--pbf",
        type=_path,
        action="append",
        help=(
            "Pinned regional .osm.pbf; repeat --pbf for adjacent extracts "
            "crossed by one network."
        ),
    )
    source_group.add_argument("--live", action="store_true")
    context_parser.add_argument("--cache-dir", type=_path, default=default_cache_dir())
    context_parser.add_argument("--endpoint", default=DEFAULT_OVERPASS_URL)
    context_parser.add_argument(
        "--source-id",
        required=True,
        help="Stable lower-case contract ID for this immutable context source.",
    )
    context_parser.add_argument(
        "--source-url",
        required=True,
        help="Stable non-file URI represented by the snapshot bytes.",
    )
    context_parser.add_argument(
        "--retrieved-at",
        required=True,
        help="Actual snapshot retrieval date in YYYY-MM-DD form.",
    )
    context_parser.add_argument("--user-agent")
    context_parser.add_argument("--timeout", type=int, default=240)
    context_parser.add_argument("--max-response-mb", type=float, default=256.0)
    context_parser.add_argument(
        "--profile",
        choices=("house", "auto", "plot", "detail"),
        default=DEFAULT_CONTEXT_PROFILE,
        help=(
            "Context density: house applies the current city/network product "
            "policy, auto preserves the legacy extent rule, plot preserves the "
            "legacy low-ink rule, and detail requests all supported layers."
        ),
    )
    context_parser.add_argument("--refresh", action="store_true")

    pen_parser = commands.add_parser(
        "pen-template",
        help="Write a local line-to-physical-pen calibration template.",
    )
    pen_parser.add_argument("contract", type=_path)
    pen_parser.add_argument("--output", type=_path, required=True)

    build = commands.add_parser(
        "build", help="Render an offline contract to SVG and plot files."
    )
    build.add_argument("contract", type=_path)
    build.add_argument("--output-dir", type=_path, required=True)
    build.add_argument(
        "--station-labels", choices=("none", "key", "all"), default="key"
    )
    build.add_argument("--pen-map", type=_path)
    build.add_argument(
        "--allow-route-only",
        action="store_true",
        help=(
            "Explicitly allow a route-only proof with no geographic house "
            "context. Ordinary customer map builds fail closed instead."
        ),
    )
    build.add_argument("--no-png", action="store_true")
    build.add_argument("--png-dpi", type=float, default=180.0)
    build.add_argument("--no-split-pens", action="store_true")
    build.add_argument(
        "--generated-at",
        type=_iso_datetime,
        help=(
            "Pin the manifest timestamp to a canonical timezone-aware ISO-8601 "
            "value for deterministic release reproduction."
        ),
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "list":
        values = [
            _catalog_summary(record)
            for _, record in sorted(load_transit_catalog().items())
        ]
        if args.json:
            _print(values)
        else:
            for value in values:
                line_word = "line" if value["line_count"] == 1 else "lines"
                print(
                    f"{value['id']:<26} {value['release_gate']:<8} "
                    f"{value['line_count']:>2} {line_word:<5}  {value['name']}"
                )
        return 0
    if args.command == "inspect":
        _print(_network_summary(args.contract))
        return 0
    if args.command == "catalog":
        _print(catalog_network(args.network_id))
        return 0
    if args.command == "acquire":
        record = catalog_network(args.network_id)
        acquisition = record.get("acquisition", {})
        mode = acquisition.get("mode") if isinstance(acquisition, dict) else None
        if mode == "osm-route-relations":
            result = acquire_osm_transit_contract(
                args.network_id,
                user_agent=user_agent_from(args.user_agent),
                cache_dir=args.cache_dir,
                output_path=args.output,
            )
        elif mode == "official-geometry-plus-gtfs":
            result = acquire_tfgm_transit_contract(
                args.network_id,
                user_agent=user_agent_from(args.user_agent),
                cache_dir=args.cache_dir,
                output_path=args.output,
            )
        else:
            raise MapPlotterError(
                f"{args.network_id} has no implemented acquisition compiler; "
                "inspect its catalog release gate and required structured source."
            )
        _print(result)
        return 0
    if args.command == "national-audit":
        record = catalog_network(args.network_id)
        acquisition = record.get("acquisition", {})
        if not isinstance(acquisition, dict) or acquisition.get("mode") != (
            "national-rail-timetable-plus-naptan"
        ):
            raise MapPlotterError(
                f"{args.network_id} is not a National Rail timetable catalog subject."
            )
        raw_codes = acquisition.get("atoc_codes")
        if not isinstance(raw_codes, list) or not raw_codes:
            raise MapPlotterError(
                f"{args.network_id} has no catalog-pinned National Rail ATOC code."
            )
        service_date = args.service_date
        if service_date is None:
            raw_service_date = acquisition.get("service_date")
            if not isinstance(raw_service_date, str):
                raise MapPlotterError(
                    f"{args.network_id} has no catalog-pinned service date."
                )
            try:
                service_date = date.fromisoformat(raw_service_date)
            except ValueError as exc:
                raise MapPlotterError(
                    f"{args.network_id} has an invalid catalog service date."
                ) from exc
        source_pack = load_national_rail_source_pack(args.source_pack)
        parsed = parse_national_rail_source_pack(source_pack)
        state = apply_schedule_transactions([parsed.cif])
        selection = select_effective_operator_schedules(
            state,
            service_date=service_date,
            atoc_codes={str(value) for value in raw_codes},
        )
        station_qa = audit_msn_stations_against_naptan(parsed.msn, parsed.naptan)
        selected_tiplocs = {
            location.tiploc
            for schedule in selection.schedules
            for location in schedule.locations
        }
        _print(
            {
                "schema_version": 1,
                "network_id": args.network_id,
                "source_pack": {
                    "manifest_path": str(source_pack.manifest_path),
                    "manifest_sha256": source_pack.manifest_sha256,
                    "sources": [
                        {
                            "role": source.role,
                            "sha256": source.sha256,
                            "byte_count": source.byte_count,
                        }
                        for source in source_pack.sources
                    ],
                },
                "timetable": {
                    "extract_start": parsed.cif.header.extract_start.isoformat(),
                    "extract_end": parsed.cif.header.extract_end.isoformat(),
                    "service_date": service_date.isoformat(),
                    "atoc_codes": list(selection.atoc_codes),
                    "source_schedule_count": len(state),
                    "selected_passenger_schedule_count": len(selection.schedules),
                    "cancelled_uid_count": len(selection.cancelled_uids),
                    "excluded_schedule_count": len(selection.exclusions),
                    "selected_timing_location_count": len(selected_tiplocs),
                },
                "station_qa": {
                    "msn_station_count": len(parsed.msn.stations),
                    "active_naptan_rail_entrance_count": len(
                        parsed.naptan.rail_entrances
                    ),
                    "matched_station_count": len(station_qa.matches),
                    "unmatched_tiploc_count": len(station_qa.unmatched_tiplocs),
                    "unmatched_tiplocs": list(station_qa.unmatched_tiplocs),
                },
                "geometry_compilation": {
                    "status": "blocked-missing-reviewed-alignment",
                    "operator_diagram_geometry_used": False,
                    "invented_connector_count": 0,
                    "required_next_input": acquisition.get(
                        "alignment_requirement"
                    ),
                    "reason": (
                        "CIF proves dated operator service calls, not the exact "
                        "physical track path between them."
                    ),
                },
            }
        )
        return 0
    if args.command == "wtt-audit":
        operator_codes = frozenset(
            args.operator_code or sorted(DEFAULT_OPERATOR_CODES)
        )
        archive = parse_wtt_archive(
            args.archive,
            expected_sha256=args.sha256,
            operator_codes=operator_codes,
        )
        schedule_counts = {
            code: sum(
                schedule.operator_code == code for schedule in archive.schedules
            )
            for code in sorted(operator_codes)
        }
        route_slice_counts = {
            code: sum(
                len(schedule.route_slices)
                for schedule in archive.schedules
                if schedule.operator_code == code
            )
            for code in sorted(operator_codes)
        }
        timing_point_counts = {
            code: sum(
                len(route_slice.timing_points)
                for schedule in archive.schedules
                if schedule.operator_code == code
                for route_slice in schedule.route_slices
            )
            for code in sorted(operator_codes)
        }
        audit = archive.audit
        _print(
            {
                "schema_version": 1,
                "source": {
                    "path": str(audit.archive_path),
                    "sha256": audit.archive_sha256,
                    "byte_count": audit.archive_byte_count,
                },
                "selected_operator_codes": sorted(operator_codes),
                "schedule_counts": schedule_counts,
                "route_slice_counts": route_slice_counts,
                "timing_point_counts": timing_point_counts,
                "archive_audit": {
                    "entry_count": len(audit.entries),
                    "workbook_count": audit.workbook_count,
                    "worksheet_count": audit.worksheet_count,
                    "selected_column_appearances": (
                        audit.selected_column_appearances
                    ),
                    "schedule_count": audit.schedule_count,
                    "route_slice_count": audit.route_slice_count,
                    "formula_cells_with_cache": audit.formula_cells_with_cache,
                    "formula_cells_without_cache": (
                        audit.formula_cells_without_cache
                    ),
                    "excluded_by_reason": dict(audit.excluded_by_reason),
                    "operator_appearances": dict(audit.operator_appearances),
                },
                "geographic_route_compiled": False,
                "invented_connector_count": 0,
            }
        )
        return 0
    if args.command == "rail-graph-audit":
        graph = load_osm_rail_graph(
            args.pbf,
            expected_sha256=args.sha256,
            required_bounds_wgs84=tuple(args.bounds),
        )
        _print(graph.audit())
        return 0
    if args.command == "operator-overview":
        audit_output = args.audit_output or _default_overview_audit_path(args.output)
        if args.output.resolve() == audit_output.resolve():
            raise MapPlotterError(
                "Operator overview contract and audit outputs must be different paths."
            )
        geometry = extract_targeted_operator_geometry(
            args.pbf,
            expected_sha256=args.sha256,
        )
        overview_audit = audit_targeted_operator_geometry(geometry)
        network = compile_operator_overview_network(
            geometry,
            overview_audit,
            snapshot_date=args.snapshot_date.isoformat(),
            retrieved_at=args.retrieved_at.isoformat(),
            require_expected_coverage=True,
        )
        _write_atomic_json(audit_output, overview_audit)
        _write_atomic_json(args.output, network.as_dict())
        blocked = [
            item["product_id"]
            for item in network.omissions
            if item.get("status") == "blocked-no-usable-osm-relation"
        ]
        partial = [
            item["product_id"]
            for item in network.omissions
            if item.get("status") == "partial-great-britain-section-only"
        ]
        _print(
            {
                "schema_version": 1,
                "network_id": network.id,
                "kind": network.kind,
                "contract": {
                    "path": str(args.output.resolve()),
                    "sha256": _sha256_path(args.output),
                },
                "evidence_audit": {
                    "path": str(audit_output.resolve()),
                    "ordered_evidence_sha256": overview_audit[
                        "ordered_evidence_sha256"
                    ],
                },
                "registry_product_count": len(network.lines) + len(blocked),
                "represented_product_count": len(network.lines),
                "blocked_product_ids": blocked,
                "partial_product_ids": partial,
                "line_count": len(network.lines),
                "atomic_edge_count": len(network.edges),
                "shared_atomic_edge_count": sum(
                    len(edge.line_ids) > 1 for edge in network.edges
                ),
                "invented_connector_count": 0,
                "context_attached": False,
                "next_step": (
                    "Attach a pinned scale-aware house context before rendering; "
                    "the contract intentionally contains route evidence only."
                ),
            }
        )
        return 0
    if args.command == "physical-rail":
        physical_rail = load_zoomstack_physical_rail(
            args.mbtiles,
            expected_sha256=args.sha256,
            zoom=args.zoom,
            national_context_zoom=args.context_zoom,
        )
        outputs = write_zoomstack_rail_plate(
            physical_rail,
            args.output_dir,
            simplify_mm=args.simplify_mm,
            png=not args.no_png,
            png_dpi=args.png_dpi,
            split_pens=not args.no_split_pens,
        )
        _print(outputs)
        return 0
    if args.command == "context":
        provenance = ContextSnapshotProvenance(
            source_id=args.source_id,
            source_url=args.source_url,
            retrieved_at=args.retrieved_at,
        )
        if args.overpass_file:
            result = attach_overpass_file(
                args.contract,
                args.overpass_file,
                output_path=args.output,
                provenance=provenance,
                profile=args.profile,
            )
        elif args.pbf:
            result = (
                attach_pbf_file(
                    args.contract,
                    args.pbf[0],
                    output_path=args.output,
                    provenance=provenance,
                    profile=args.profile,
                )
                if len(args.pbf) == 1
                else attach_pbf_files(
                    args.contract,
                    args.pbf,
                    output_path=args.output,
                    provenance=provenance,
                    profile=args.profile,
                )
            )
        else:
            acquisition, bbox, layers = fetch_transit_context(
                args.contract,
                user_agent=user_agent_from(args.user_agent),
                cache_dir=args.cache_dir,
                endpoint=args.endpoint,
                timeout_s=args.timeout,
                refresh=args.refresh,
                max_response_mb=args.max_response_mb,
                profile=args.profile,
            )
            if not acquisition.cache_path:
                raise MapPlotterError(
                    "Live transit context did not produce a pinned cache file."
                )
            result = attach_context(
                args.contract,
                acquisition,
                bbox=bbox,
                enabled_layers=layers,
                source_path=Path(acquisition.cache_path),
                output_path=args.output,
                provenance=provenance,
                context_profile=args.profile,
            )
        _print(result)
        return 0
    if args.command == "pen-template":
        value = _pen_template(args.contract)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _print({"path": str(args.output.resolve()), "line_count": len(value["lines"])})
        return 0
    if args.command == "build":
        network = apply_pen_map(load_transit_network(args.contract), args.pen_map)
        if not network.context and not args.allow_route_only:
            raise MapPlotterError(
                "Transit map builds require pinned geographic context by default. "
                "Run `mapplot-transit context ... --profile house` first; use "
                "--allow-route-only only for an explicitly non-map proof."
            )
        outputs = write_transit_plate(
            network,
            args.output_dir,
            station_label_policy=args.station_labels,
            png=not args.no_png,
            png_dpi=args.png_dpi,
            split_pens=not args.no_split_pens,
            generated_at=args.generated_at,
            allow_route_only=args.allow_route_only,
        )
        _print(outputs)
        return 0
    raise MapPlotterError(f"Unsupported command {args.command!r}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        return _run(parser.parse_args(argv))
    except MapPlotterError as exc:
        print(f"mapplot-transit: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
