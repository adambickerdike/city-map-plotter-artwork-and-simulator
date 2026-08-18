#!/usr/bin/env python3
"""Compile frozen former-F1 source records into normalized circuit models."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from shapely.geometry import LineString

from city_map_plotter.f1_circuits import validate_f1_event

try:  # script execution puts ``tools`` itself on sys.path
    from tools import build_f1_circuit_catalog as base
except ModuleNotFoundError:  # pragma: no cover - direct script path
    import build_f1_circuit_catalog as base


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_ROOT = ROOT / "contracts" / "f1-circuits-legacy-v1"
REGISTRY_PATH = CONTRACT_ROOT / "event-registry.json"
MANIFEST_PATH = CONTRACT_ROOT / "source-manifest.json"
OUTPUT_PATH = (
    ROOT / "src" / "city_map_plotter" / "data" / "f1-circuits-legacy-v1.json"
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _portable(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_uri()


def _verify_source(source: dict[str, Any]) -> bytes:
    path = ROOT / str(source.get("path") or "")
    compressed = path.read_bytes()
    if _sha256(compressed) != source.get("compressed_sha256"):
        raise ValueError(f"compressed hash mismatch for {source.get('id')}")
    payload = gzip.decompress(compressed)
    if _sha256(payload) != source.get("payload_sha256"):
        raise ValueError(f"payload hash mismatch for {source.get('id')}")
    if len(payload) != source.get("payload_bytes"):
        raise ValueError(f"payload byte count mismatch for {source.get('id')}")
    for component in source.get("component_snapshots", []):
        component_path = ROOT / str(component.get("path") or "")
        component_compressed = component_path.read_bytes()
        if _sha256(component_compressed) != component.get("compressed_sha256"):
            raise ValueError(
                f"component compressed hash mismatch for {source.get('id')}"
            )
        component_payload = gzip.decompress(component_compressed)
        if _sha256(component_payload) != component.get("payload_sha256"):
            raise ValueError(
                f"component payload hash mismatch for {source.get('id')}"
            )
    return payload


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "source_kind",
        "publisher",
        "title",
        "url",
        "path",
        "media_type",
        "retrieved_at",
        "payload_bytes",
        "payload_sha256",
        "compressed_bytes",
        "compressed_sha256",
        "licence",
        "commercial_use_status",
        "allowed_uses",
        "attribution",
        "event_id",
        "selection",
        "selected_objects",
        "context_bbox",
        "element_count",
        "geometry_derivation_status",
        "evidence_scope",
    }
    result = {key: source[key] for key in source if key in allowed}
    result["sha256"] = source["payload_sha256"]
    return result


def _source_objects(
    relation: dict[str, Any] | None,
    selected: list[tuple[dict[str, Any], str]],
    used_way_ids: list[int],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if relation is not None:
        result.append(base._source_object(relation))
    by_id = {int(way["id"]): way for way, _ in selected}
    result.extend(
        base._source_object(by_id[way_id])
        for way_id in used_way_ids
        if way_id in by_id
    )
    return result


def _hold_event(event: dict[str, Any]) -> dict[str, Any]:
    reference_season = int(event["configuration_reference_season"])
    source_refs = list(event["configuration_identity"].get("source_refs", []))
    official_length = event.get("published_length_m")
    length_source_ref = event.get("length_source_ref")
    return {
        "id": event["id"],
        "calendar_order": event["calendar_order"],
        "calendar_status": "historic-reference-hold",
        "event_identity": event["event_identity"],
        "neutral_display_title": event["neutral_display_title"],
        "event_country_iso2": event["event_country_iso2"],
        "host_country_iso2": event["host_country_iso2"],
        "host_city": str(event["location_label"]).split(",", 1)[0],
        "location": event["location_label"],
        "configuration_reference_season": reference_season,
        "render_disclosure": event.get(
            "render_disclosure",
            f"HISTORIC CONFIGURATION HELD / F1 REFERENCE {reference_season}",
        ),
        "configuration_identity": event["configuration_identity"],
        "circuit": {
            "id": str(event["id"]).removesuffix(f"-{reference_season}"),
            "official_name": event["circuit_name"],
            "name": event["circuit_name"],
            "location_label": event["location_label"],
            "site_type": event["site_type"],
            "configuration_id": f"f1-reference-{reference_season}",
            "configuration_season": reference_season,
            "direction": "withheld",
            "lap_direction": "withheld",
            "lap_direction_status": "withheld-no-qualified-geometry",
            "lap_length_m": official_length,
            "published_length_km": (
                float(official_length) / 1000.0
                if isinstance(official_length, (int, float))
                else None
            ),
            "geometry": {
                "status": "unavailable",
                "model": None,
                "official_centreline_length_m": {
                    "value": official_length,
                    "source_ref": length_source_ref,
                },
                "review": {
                    "status": "held",
                    "closed_lap": False,
                    "findings": list(event.get("hold_reasons", [])),
                },
            },
        },
        "sources": {"configuration_source_refs": source_refs},
        "rights": {
            "geometry_commercial_use_status": "unresolved",
            "context_commercial_use_status": "unresolved",
            "release_gate": "hold",
            "required_attribution": [],
        },
        "review": {
            "catalog_build_status": "held",
            "production_ready": False,
            "hold_reasons": list(event.get("hold_reasons", [])),
        },
    }


def _build_geometry_event(
    event: dict[str, Any],
    *,
    source: dict[str, Any],
    payload: bytes,
    length_threshold_percent: float,
) -> dict[str, Any]:
    source_ref = str(source["id"])
    snapshot = json.loads(payload)
    index = base._element_index(snapshot)
    selected, pits, relation, selection_findings = base._selection_ways(event, index)
    raw_lap, used_ids, closed, assembly_method, assembly_findings = base._assemble_lap(
        selected
    )
    findings = [*selection_findings, *assembly_findings]
    all_coordinates = [
        coordinate
        for way, _ in selected
        for coordinate in base._way_coordinates(way)
    ]
    origin_lon, origin_lat = base._origin(all_coordinates or raw_lap)
    project = base._projector(origin_lon, origin_lat)
    projected_lap = [project(value) for value in raw_lap]
    lap_line = LineString(projected_lap) if len(projected_lap) >= 2 else LineString()
    measured_length_m = (
        round(float(lap_line.length), 3) if not lap_line.is_empty else None
    )
    published_length_m = float(event["published_length_m"])
    discrepancy_m = (
        round(measured_length_m - published_length_m, 3)
        if measured_length_m is not None
        else None
    )
    discrepancy_percent = (
        round(abs(discrepancy_m) / published_length_m * 100.0, 6)
        if discrepancy_m is not None
        else None
    )
    if not closed:
        findings.append("lap is not one exact-endpoint closed cycle")
    if (
        discrepancy_percent is not None
        and discrepancy_percent > length_threshold_percent
    ):
        findings.append(
            "measured OSM lap differs from published reference length by "
            f"{discrepancy_percent:.6f}%"
        )
    source_objects = _source_objects(relation, selected, used_ids)
    lap_feature = (
        base._line_feature(
            feature_id=f"{event['id']}-lap",
            coordinates=projected_lap,
            source_ref=source_ref,
            source_objects=source_objects,
            properties={
                "claim_scope": "osm-centreline-exact-selected-configuration",
                "width_status": "centreline-only",
                "closed_lap": closed,
                "assembly_method": assembly_method,
            },
        )
        if len(projected_lap) >= 2
        else None
    )
    selected_relation_id = int(relation["id"]) if relation is not None else None
    pit_features, pit_findings = base._pit_features(
        pits,
        project=project,
        source_ref=source_ref,
        lap=lap_line,
        lap_coordinates=raw_lap,
    )
    findings.extend(pit_findings)
    pit_topology_valid = bool(pit_features) and not pit_findings
    context, boundaries = base._context_features(
        index=index,
        selected_way_ids={int(way["id"]) for way, _ in selected},
        pit_way_ids={int(way["id"]) for way in pits},
        selected_relation_id=selected_relation_id,
        project=project,
        source_ref=source_ref,
    )
    context, context_selection = base._prune_context_features(context, lap=lap_line)
    reference_season = int(event["configuration_reference_season"])
    for feature in context:
        if feature.get("kind") == "grandstand":
            # The shared compiler's frozen current-OSM observation contract is
            # intentionally retained.  A present-day stand footprint is not
            # evidence that the same stand existed, or was configured for an
            # event, in the reference season.
            feature["valid_for_season"] = None
        else:
            feature["valid_for_season"] = reference_season
            feature["temporary_status"] = (
                "current-osm-context-for-reference-artwork-not-historic-existence-claim"
            )
            feature["source_temporality"] = "snapshot-current-not-backdated"
    special_sections = base._special_sections(
        selected,
        project=project,
        source_ref=source_ref,
        generic_course_names=(event["circuit_name"],),
    )
    for section in special_sections:
        section["valid_for_season"] = reference_season
        section["source_temporality"] = "snapshot-current-name-not-backdated"
    centreline_qualified = (
        closed
        and lap_feature is not None
        and bool(source_objects)
        and discrepancy_percent is not None
        and discrepancy_percent <= length_threshold_percent
    )
    geometry_review = {
        "status": "passed" if centreline_qualified else "held",
        "qualification_tier": (
            "cartography-qualified-centreline"
            if centreline_qualified
            else "provisional"
        ),
        "configuration_identity_status": event["configuration_identity"]["status"],
        "method": assembly_method,
        "selected_relation_id": selected_relation_id,
        "selected_way_ids": [int(way["id"]) for way, _ in selected],
        "used_way_ids": used_ids,
        "closed_lap": closed,
        "published_length_m": published_length_m,
        "measured_length_m": measured_length_m,
        "length_discrepancy_m": discrepancy_m,
        "length_discrepancy_percent": discrepancy_percent,
        "review_threshold_percent": length_threshold_percent,
        "context_selection": context_selection,
        "named_source_section_count": sum(
            section.get("kind") == "named-course-section"
            for section in special_sections
        ),
        "pit_lane_topology_status": (
            "passed-exact-endpoint-open-chain-v1"
            if pit_topology_valid
            else "withheld"
        ),
        "findings": findings,
    }
    model: dict[str, Any] | None = None
    if centreline_qualified:
        model = {
            "model_version": 1,
            "coordinate_system": "local-metre",
            "origin_wgs84": [origin_lon, origin_lat],
            "coordinate_space": "local-metres",
            "projection_metadata": {
                "longitude": origin_lon,
                "latitude": origin_lat,
                "source_crs": "EPSG:4326",
                "projection": "local-equirectangular-v1",
                "earth_radius_m": base.EARTH_RADIUS_M,
                "x_axis": "east",
                "y_axis": "north",
                "units": "m",
            },
            "lap": lap_feature,
            "lap_source_objects": source_objects,
            "pit_lanes": pit_features if pit_topology_valid else [],
            "track_boundaries": boundaries,
            "context": context,
            "turn_stations": [],
            "turn_inventory": {
                "status": "withheld-no-official-coordinate-bearing-turn-inventory",
                "official_count": None,
                "official_numbering_verified": False,
                "apex_inventory_verified": False,
                "claim_scope": "named-source-sections-are-not-turn-or-apex-claims",
            },
            "start_finish": None,
            "special_sections": special_sections,
            "operational_overlays": {
                "status": "withheld-no-reference-season-event-document",
                "straight_mode_zones": [],
                "overtake_detection_points": [],
                "overtake_activation_points": [],
                "speed_traps": [],
                "intermediate_timing_lines": [],
            },
            "qualification": {
                "tier": "cartography-qualified-centreline",
                "claim_scope": (
                    "source-qualified-centreline-cartography-only; current OSM "
                    "context is not a historical reconstruction"
                ),
                "omitted_capabilities": [
                    "source-backed-start-finish-anchor",
                    "source-backed-turn-or-apex-inventory",
                    "reference-season-operational-overlays",
                    "historic-context-reconstruction",
                ],
                "omissions_must_be_visibly_disclosed": True,
            },
            "source_ref": source_ref,
            "assembly": geometry_review,
        }
        model["geometry_sha256"] = base._geometry_sha256(model)

    identity_refs = list(event["configuration_identity"].get("source_refs", []))
    status = "cartography-qualified-centreline" if model is not None else "provisional"
    output = {
        "id": event["id"],
        "calendar_order": event["calendar_order"],
        "calendar_status": "historic-reference",
        "event_identity": event["event_identity"],
        "neutral_display_title": event["neutral_display_title"],
        "event_country_iso2": event["event_country_iso2"],
        "host_country_iso2": event["host_country_iso2"],
        "host_city": str(event["location_label"]).split(",", 1)[0],
        "location": event["location_label"],
        "configuration_reference_season": reference_season,
        "render_disclosure": event["render_disclosure"],
        "configuration_identity": event["configuration_identity"],
        "circuit": {
            "id": str(event["id"]).removesuffix(f"-{reference_season}"),
            "official_name": event["circuit_name"],
            "name": event["circuit_name"],
            "location_label": event["location_label"],
            "site_type": event["site_type"],
            "configuration_id": f"f1-reference-{reference_season}",
            "configuration_season": reference_season,
            "direction": event.get("lap_direction", "withheld"),
            "lap_direction": event.get("lap_direction", "withheld"),
            "lap_direction_status": "source-backed-reference-configuration",
            "lap_direction_source_ref": event.get("lap_direction_source_ref"),
            "lap_length_m": published_length_m,
            "published_length_km": published_length_m / 1000.0,
            "geometry": {
                "status": status,
                "model": model,
                "source_ref": source_ref,
                "official_centreline_length_m": {
                    "value": published_length_m,
                    "source_ref": event["length_source_ref"],
                },
                "review": geometry_review,
            },
        },
        "sources": {
            "geometry_and_context_ref": source_ref,
            "configuration_source_refs": identity_refs,
            "length_source_ref": event["length_source_ref"],
        },
        "rights": {
            "geometry_commercial_use_status": "conditional-ODbL-produced-work",
            "context_commercial_use_status": "conditional-ODbL-produced-work",
            "release_gate": "hold-rights-and-physical-proof",
            "official_page_use": "reference-only-factual-transcription",
            "required_attribution": [
                "© OpenStreetMap contributors",
                "https://www.openstreetmap.org/copyright",
            ],
        },
        "review": {
            "catalog_build_status": (
                "geometry-verified-centreline" if model is not None else "held"
            ),
            "production_ready": False,
            "hold_reasons": [
                *([] if model is not None else findings),
                "circuit-owner outline rights require legal clearance",
                "physical pen calibration and plotted proof remain outstanding",
            ],
            "current_context_historic_claim": False,
            "operational_overlay_status": "withheld",
        },
    }
    return output


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    registry_bytes = args.registry.read_bytes()
    manifest_bytes = args.manifest.read_bytes()
    registry = json.loads(registry_bytes)
    manifest = json.loads(manifest_bytes)
    expected_registry_hash = manifest.get("freeze", {}).get(
        "event_registry_sha256"
    )
    if _sha256(registry_bytes) != expected_registry_hash:
        raise SystemExit("event registry hash does not match source manifest")
    records = {
        str(source["id"]): source for source in manifest.get("sources", [])
    }
    payloads: dict[str, bytes] = {}
    source_errors: list[str] = []
    for source_id, source in records.items():
        try:
            payloads[source_id] = _verify_source(source)
        except Exception as exc:
            source_errors.append(f"{source_id}: {type(exc).__name__}: {exc}")
    threshold = float(registry.get("length_review_threshold_percent", 1.0))
    events: list[dict[str, Any]] = []
    for raw_event in registry["events"]:
        if raw_event.get("release_status") == "hold":
            events.append(_hold_event(raw_event))
            continue
        source_id = f"osm-circuit-context-{raw_event['id']}"
        source = records.get(source_id)
        payload = payloads.get(source_id)
        if source is None or payload is None:
            held = dict(raw_event)
            held["release_status"] = "hold"
            held["hold_reasons"] = ["frozen OSM source snapshot is unavailable"]
            events.append(_hold_event(held))
            continue
        events.append(
            _build_geometry_event(
                raw_event,
                source=source,
                payload=payload,
                length_threshold_percent=threshold,
            )
        )

    # Validate every event against its own reference season. The shared catalog
    # validator provides the separate multi-era bridge; this remains useful as
    # a fail-closed check even before that catalog wrapper is imported.
    source_registry = {source_id: value for source_id, value in records.items()}
    for event in events:
        validate_f1_event(
            event,
            source_registry=source_registry,
            season=int(event["configuration_reference_season"]),
        )

    renderable = [
        event["id"]
        for event in events
        if isinstance(event["circuit"]["geometry"].get("model"), dict)
    ]
    held = [event["id"] for event in events if event["id"] not in renderable]
    catalog = {
        "schema_version": 1,
        "catalog_id": "f1-circuits-legacy-v1",
        "catalog_class": "legacy-f1-configurations",
        "season_scope": "multi-era",
        "season": int(registry["season"]),
        "freeze": {
            "frozen_at": registry["frozen_at"],
            "event_registry_path": _portable(args.registry),
            "event_registry_sha256": _sha256(registry_bytes),
            "source_manifest_path": _portable(args.manifest),
            "source_manifest_sha256": _sha256(manifest_bytes),
            "geometry_review_summary": {
                "event_count": len(events),
                "renderable_centreline_count": len(renderable),
                "renderable_centreline_ids": renderable,
                "held_count": len(held),
                "held_ids": held,
                "source_verification_errors": source_errors,
                "acquisition_errors": manifest.get("acquisition_errors", []),
            },
            "geometry_policy": (
                "exact-frozen-osm-relation-or-ordered-ways; no inferred connectors; "
                "official/reference length discrepancy at or below 1.0 percent"
            ),
            "context_temporality_policy": (
                "current OSM context is visibly disclosed and never asserted as "
                "reference-season historic reconstruction"
            ),
        },
        "sources": [
            _public_source(source)
            for source in sorted(records.values(), key=lambda value: value["id"])
        ],
        "events": events,
        "excluded_calendar_events": registry.get("excluded_calendar_events", []),
    }
    output_bytes = _pretty_json(catalog)
    if args.check:
        if not args.output.is_file():
            raise SystemExit(f"cannot check missing output: {args.output}")
        existing = args.output.read_bytes()
        if existing != output_bytes:
            raise SystemExit(
                "offline rebuild differs from packaged legacy catalog: "
                f"{_sha256(existing)} != {_sha256(output_bytes)}"
            )
        print(f"deterministic legacy catalog match: {_sha256(existing)}")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output_bytes)
    print(
        f"wrote {args.output}: {len(renderable)} renderable / {len(held)} held / "
        f"sha256={_sha256(output_bytes)}"
    )
    if held:
        print("held: " + ", ".join(held))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
