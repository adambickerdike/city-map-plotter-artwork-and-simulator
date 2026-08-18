#!/usr/bin/env python3
"""Acquire immutable official-page, official-document, and OSM evidence.

This maintenance tool is deliberately separate from rendering.  It stores the
exact HTML/JSON bytes used for factual transcription and geographic geometry,
plus hashes, retrieval times, query text, source rights, and OSM base dates.
Reference-only FIA event PDFs may be frozen for factual transcription (for
example, their issue date and published turn count).  The tool never downloads
standalone circuit-map image assets and never traces or derives geometry from
an F1/FIA graphic.

Examples::

    .venv/bin/python tools/acquire_f1_circuit_sources.py --all
    .venv/bin/python tools/acquire_f1_circuit_sources.py \
        --event australia-albert-park-2026 --refresh

Existing snapshots are reused unless ``--refresh`` is supplied.  A partial
network failure is recorded in ``source-manifest.json`` and does not create a
placeholder payload; the offline builder will hold that event explicitly.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
CONTRACT_ROOT = ROOT / "contracts" / "f1-circuits-2026"
REGISTRY_PATH = CONTRACT_ROOT / "event-registry.json"
MANIFEST_PATH = CONTRACT_ROOT / "source-manifest.json"
OFFICIAL_ROOT = CONTRACT_ROOT / "source-extracts" / "official"
OSM_ROOT = CONTRACT_ROOT / "source-extracts" / "osm"

CONTRACT_ID = "f1-circuits-2026-sources-v1"
SCHEMA_VERSION = 1
F1_RACE_PAGE = "https://www.formula1.com/en/racing/2026/{slug}"
DEFAULT_USER_AGENT = (
    "city-map-plotter-f1-source-builder/1.0 "
    "(+https://www.openstreetmap.org/copyright)"
)
OVERPASS_ENDPOINTS = (
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_gzip(path: Path, payload: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    path.write_bytes(compressed)
    return compressed


def _read_gzip(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _fetch(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    attempts: int = 4,
) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.geturl()
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(20.0, 2.0 ** attempt * 2.0))
    assert last_error is not None
    raise last_error


def _track_query(event: dict[str, Any]) -> str:
    selection = event["osm_selection"]
    if selection["mode"] == "relation":
        relation_id = int(selection["relation_id"])
        return (
            "[out:json][timeout:120];"
            f"relation({relation_id})->.circuit;"
            "(.circuit;way(r.circuit);node(r.circuit););"
            "out meta geom qt;"
        )
    elif selection["mode"] == "explicit-ordered-way-list":
        way_ids = ",".join(str(int(value)) for value in selection["way_ids"])
        return (
            "[out:json][timeout:120];"
            f"way(id:{way_ids})->.track;"
            "(.track;>;);out meta geom qt;"
        )
    else:
        raise ValueError(
            f"Unsupported OSM selection mode for {event['id']}: "
            f"{selection['mode']!r}"
        )



def _geometry_bbox(snapshot: dict[str, Any], padding_m: float = 650.0) -> str:
    coordinates: list[tuple[float, float]] = []
    for element in snapshot.get("elements", []):
        if not isinstance(element, dict):
            continue
        if isinstance(element.get("lat"), (int, float)) and isinstance(
            element.get("lon"), (int, float)
        ):
            coordinates.append((float(element["lat"]), float(element["lon"])))
        for value in element.get("geometry", []):
            if isinstance(value, dict) and isinstance(
                value.get("lat"), (int, float)
            ) and isinstance(value.get("lon"), (int, float)):
                coordinates.append((float(value["lat"]), float(value["lon"])))
        for member in element.get("members", []):
            if not isinstance(member, dict):
                continue
            if isinstance(member.get("lat"), (int, float)) and isinstance(
                member.get("lon"), (int, float)
            ):
                coordinates.append((float(member["lat"]), float(member["lon"])))
            for value in member.get("geometry", []):
                if isinstance(value, dict) and isinstance(
                    value.get("lat"), (int, float)
                ) and isinstance(value.get("lon"), (int, float)):
                    coordinates.append((float(value["lat"]), float(value["lon"])))
    if not coordinates:
        raise ValueError("Selected OSM circuit source contains no coordinates")
    south = min(value[0] for value in coordinates)
    north = max(value[0] for value in coordinates)
    west = min(value[1] for value in coordinates)
    east = max(value[1] for value in coordinates)
    centre_latitude = (south + north) / 2.0
    latitude_padding = padding_m / 111_320.0
    longitude_padding = padding_m / max(
        1.0, 111_320.0 * math.cos(math.radians(centre_latitude))
    )
    return (
        f"{south - latitude_padding:.7f},"
        f"{west - longitude_padding:.7f},"
        f"{north + latitude_padding:.7f},"
        f"{east + longitude_padding:.7f}"
    )


def _context_query(bbox: str) -> str:
    # A bbox derived from the exact selected lap is substantially cheaper and
    # more reproducible than repeated ``around`` traversal over every member.
    return (
        "[out:json][timeout:240];("
        + f'node["raceway"]({bbox});'
        + f'node["motor_racing"]({bbox});'
        + f'way["highway"]({bbox});'
        + f'way["raceway"]({bbox});'
        + f'relation["raceway"]({bbox});'
        + f'way["motor_racing"]({bbox});'
        + f'relation["motor_racing"]({bbox});'
        + f'way["area:highway"="raceway"]({bbox});'
        + f'relation["area:highway"="raceway"]({bbox});'
        + f'way["building"]({bbox});'
        + f'relation["building"]({bbox});'
        + f'way["landuse"="paddock"]({bbox});'
        + f'relation["landuse"="paddock"]({bbox});'
        + f'way["amenity"="paddock"]({bbox});'
        + f'relation["amenity"="paddock"]({bbox});'
        + f'way["landuse"~"^(grass|meadow|forest|reservoir|basin|recreation_ground)$"]({bbox});'
        + f'relation["landuse"~"^(grass|meadow|forest|reservoir|basin|recreation_ground)$"]({bbox});'
        + f'way["natural"~"^(water|wood|scrub|wetland|coastline|bay)$"]({bbox});'
        + f'relation["natural"~"^(water|wood|scrub|wetland|bay)$"]({bbox});'
        + f'way["waterway"]({bbox});'
        + f'relation["waterway"]({bbox});'
        + f'way["leisure"~"^(park|garden)$"]({bbox});'
        + f'relation["leisure"~"^(park|garden)$"]({bbox});'
        + ");out meta geom qt;"
    )


def _fetch_overpass(
    query: str,
    *,
    endpoints: Iterable[str],
    user_agent: str,
    timeout: float,
) -> tuple[bytes, str]:
    errors: list[str] = []
    encoded = urllib.parse.urlencode({"data": query})
    for endpoint in endpoints:
        url = f"{endpoint}?{encoded}"
        try:
            payload, _ = _fetch(
                url,
                user_agent=user_agent,
                timeout=timeout,
                attempts=2,
            )
            parsed = json.loads(payload)
            if not isinstance(parsed, dict) or not isinstance(
                parsed.get("elements"), list
            ):
                raise ValueError("Overpass response is not an element collection")
            return payload, endpoint
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def _snapshot_entry(
    *,
    source_id: str,
    source_kind: str,
    publisher: str,
    title: str,
    url: str,
    path: Path,
    media_type: str,
    retrieved_at: str,
    payload: bytes,
    compressed: bytes,
    commercial_use_status: str,
    allowed_uses: list[str],
    licence: str,
    terms_url: str,
    attribution: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": source_id,
        "source_kind": source_kind,
        "publisher": publisher,
        "title": title,
        "url": url,
        "path": _relative(path),
        "media_type": media_type,
        "retrieved_at": retrieved_at,
        "payload_bytes": len(payload),
        "payload_sha256": _sha256(payload),
        "compressed_bytes": len(compressed),
        "compressed_sha256": _sha256(compressed),
        "licence": licence,
        "terms_url": terms_url,
        "commercial_use_status": commercial_use_status,
        "allowed_uses": allowed_uses,
        "attribution": attribution,
    }
    if extra:
        entry.update(extra)
    return entry


def _reuse_entry(
    existing: dict[str, dict[str, Any]], source_id: str
) -> dict[str, Any] | None:
    entry = existing.get(source_id)
    if entry is None or not entry.get("path"):
        return None
    path = ROOT / str(entry["path"])
    if not path.is_file():
        return None
    compressed = path.read_bytes()
    payload = gzip.decompress(compressed)
    if _sha256(compressed) != entry.get("compressed_sha256"):
        return None
    if _sha256(payload) != entry.get("payload_sha256"):
        return None
    for component in entry.get("component_snapshots", []):
        if not isinstance(component, dict) or not component.get("path"):
            return None
        component_path = ROOT / str(component["path"])
        if not component_path.is_file():
            return None
        component_compressed = component_path.read_bytes()
        if _sha256(component_compressed) != component.get("compressed_sha256"):
            return None
        component_payload = gzip.decompress(component_compressed)
        if _sha256(component_payload) != component.get("payload_sha256"):
            return None
    return entry


def _official_source_entry(
    source: dict[str, Any],
    *,
    existing: dict[str, dict[str, Any]],
    refresh: bool,
    user_agent: str,
    timeout: float,
) -> dict[str, Any]:
    source_id = str(source["id"])
    if not refresh:
        reused = _reuse_entry(existing, source_id)
        if reused is not None:
            return reused
    url = str(source["url"])
    request_user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/139.0 Safari/537.36 CityMapPlotterFactualAudit/1.0"
        if urllib.parse.urlparse(url).netloc == "corp.formula1.com"
        else user_agent
    )
    payload, final_url = _fetch(
        url, user_agent=request_user_agent, timeout=timeout
    )
    media_type = str(source.get("media_type") or "text/html")
    extension = str(
        source.get("file_extension")
        or ("pdf" if media_type == "application/pdf" else "html")
    ).lstrip(".")
    path = OFFICIAL_ROOT / f"{_safe_name(source_id)}.{extension}.gz"
    compressed = _write_gzip(path, payload)
    publisher = str(source["publisher"])
    extra = {
        key: source[key]
        for key in (
            "event_id",
            "document_version",
            "issued_on",
            "geometry_derivation_status",
            "evidence_scope",
        )
        if source.get(key) is not None
    }
    return _snapshot_entry(
        source_id=source_id,
        source_kind=str(source["source_kind"]),
        publisher=publisher,
        title=str(source["title"]),
        url=final_url,
        path=path,
        media_type=media_type,
        retrieved_at=_utc_now(),
        payload=payload,
        compressed=compressed,
        commercial_use_status=str(
            source.get("commercial_use_status") or "reference-only"
        ),
        allowed_uses=list(
            source.get("allowed_uses")
            or ["factual-transcription", "source-validation"]
        ),
        licence=str(
            source.get("licence") or "all-rights-reserved-reference-only"
        ),
        terms_url=str(source.get("terms_url") or (
            "https://www.formula1.com/en/information/legal-notices."
            "7egvZU48hzrypubGBNcQKt"
            if publisher == "Formula 1"
            else (
                "https://www.fia.com/sites/default/files/basicpage/file/"
                "Terms%20and%20Conditions%20FIA%20Website.pdf"
                if publisher == "Federation Internationale de l'Automobile"
                else url
            )
        )),
        attribution=str(source.get("attribution") or publisher),
        extra=extra,
    )


def _race_page_entry(
    event: dict[str, Any],
    *,
    existing: dict[str, dict[str, Any]],
    refresh: bool,
    user_agent: str,
    timeout: float,
) -> dict[str, Any]:
    source_id = f"f1-race-page-{event['id']}"
    if not refresh:
        reused = _reuse_entry(existing, source_id)
        if reused is not None:
            return reused
    url = F1_RACE_PAGE.format(slug=event["f1_page_slug"])
    payload, final_url = _fetch(url, user_agent=user_agent, timeout=timeout)
    path = OFFICIAL_ROOT / f"{event['id']}-race-page.html.gz"
    compressed = _write_gzip(path, payload)
    return _snapshot_entry(
        source_id=source_id,
        source_kind="official-race-page",
        publisher="Formula 1",
        title=f"Official 2026 race page: {event['event_identity']}",
        url=final_url,
        path=path,
        media_type="text/html",
        retrieved_at=_utc_now(),
        payload=payload,
        compressed=compressed,
        commercial_use_status="reference-only",
        allowed_uses=["factual-transcription", "source-validation"],
        licence="all-rights-reserved-reference-only",
        terms_url=(
            "https://www.formula1.com/en/information/legal-notices."
            "7egvZU48hzrypubGBNcQKt"
        ),
        attribution="Formula 1",
        extra={"event_id": event["id"]},
    )


def _osm_entry(
    event: dict[str, Any],
    *,
    existing: dict[str, dict[str, Any]],
    refresh: bool,
    endpoints: tuple[str, ...],
    user_agent: str,
    timeout: float,
) -> dict[str, Any]:
    source_id = f"osm-circuit-context-{event['id']}"
    if not refresh:
        reused = _reuse_entry(existing, source_id)
        if reused is not None:
            return reused
    track_query = _track_query(event)
    track_payload, track_endpoint = _fetch_overpass(
        track_query,
        endpoints=endpoints,
        user_agent=user_agent,
        timeout=timeout,
    )
    track_snapshot = json.loads(track_payload)
    bbox = _geometry_bbox(track_snapshot)
    context_query = _context_query(bbox)
    context_payload, context_endpoint = _fetch_overpass(
        context_query,
        endpoints=endpoints,
        user_agent=user_agent,
        timeout=timeout,
    )
    context_snapshot = json.loads(context_payload)

    # Preserve both raw server responses beside the deterministic union.  The
    # manifest binds their exact bytes, queries, endpoints, and OSM base dates.
    component_root = OSM_ROOT / "components"
    component_root.mkdir(parents=True, exist_ok=True)
    track_path = component_root / f"{event['id']}-track.raw.json.gz"
    context_path = component_root / f"{event['id']}-context.raw.json.gz"
    track_compressed = _write_gzip(track_path, track_payload)
    context_compressed = _write_gzip(context_path, context_payload)

    merged_elements: dict[tuple[str, int], dict[str, Any]] = {}
    for snapshot in (context_snapshot, track_snapshot):
        for element in snapshot.get("elements", []):
            if not isinstance(element, dict):
                continue
            element_type = element.get("type")
            element_id = element.get("id")
            if isinstance(element_type, str) and isinstance(element_id, int):
                # Track response is visited second and wins, preserving exact
                # selected-relation member roles and geometries.
                merged_elements[(element_type, element_id)] = element
    type_order = {"node": 0, "way": 1, "relation": 2}
    component_timestamps = [
        snapshot.get("osm3s", {}).get("timestamp_osm_base")
        for snapshot in (track_snapshot, context_snapshot)
        if isinstance(snapshot.get("osm3s"), dict)
    ]
    timestamps = sorted(value for value in component_timestamps if value)
    parsed = {
        "version": 0.6,
        "generator": "city-map-plotter-f1-source-builder/1.0",
        "osm3s": {
            "timestamp_osm_base": timestamps[-1] if timestamps else None,
            "copyright": "The data included in this document is from www.openstreetmap.org. The data is made available under ODbL.",
        },
        "elements": [
            merged_elements[key]
            for key in sorted(
                merged_elements,
                key=lambda value: (type_order.get(value[0], 9), value[1]),
            )
        ],
        "_city_map_plotter": {
            "merge_recipe": "exact-track-plus-derived-bbox-context-union-v1",
            "context_bbox": bbox,
            "component_payload_sha256": {
                "track": _sha256(track_payload),
                "context": _sha256(context_payload),
            },
        },
    }
    payload = _canonical_json(parsed)
    path = OSM_ROOT / f"{event['id']}-overpass-v1.json.gz"
    compressed = _write_gzip(path, payload)
    osm3s = parsed.get("osm3s") if isinstance(parsed.get("osm3s"), dict) else {}
    return _snapshot_entry(
        source_id=source_id,
        source_kind="osm-overpass-snapshot",
        publisher="OpenStreetMap contributors",
        title=f"OSM circuit and context snapshot: {event['circuit_name']}",
        url=context_endpoint,
        path=path,
        media_type="application/json",
        retrieved_at=_utc_now(),
        payload=payload,
        compressed=compressed,
        commercial_use_status="conditional",
        allowed_uses=["geometry-derived-produced-work", "factual-transcription"],
        licence="ODbL-1.0",
        terms_url="https://www.openstreetmap.org/copyright",
        attribution="© OpenStreetMap contributors",
        extra={
            "event_id": event["id"],
            "query": {
                "track": track_query,
                "context": context_query,
            },
            "query_sha256": _sha256(
                _canonical_json(
                    {"track": track_query, "context": context_query}
                )
            ),
            "osm_base_timestamp": osm3s.get("timestamp_osm_base"),
            "element_count": len(parsed["elements"]),
            "selection": event["osm_selection"],
            "context_bbox": bbox,
            "component_snapshots": [
                {
                    "role": "selected-track",
                    "path": _relative(track_path),
                    "url": track_endpoint,
                    "query": track_query,
                    "query_sha256": _sha256(track_query.encode("utf-8")),
                    "payload_bytes": len(track_payload),
                    "payload_sha256": _sha256(track_payload),
                    "compressed_bytes": len(track_compressed),
                    "compressed_sha256": _sha256(track_compressed),
                    "osm_base_timestamp": track_snapshot.get("osm3s", {}).get(
                        "timestamp_osm_base"
                    ),
                },
                {
                    "role": "derived-bbox-context",
                    "path": _relative(context_path),
                    "url": context_endpoint,
                    "query": context_query,
                    "query_sha256": _sha256(context_query.encode("utf-8")),
                    "payload_bytes": len(context_payload),
                    "payload_sha256": _sha256(context_payload),
                    "compressed_bytes": len(context_compressed),
                    "compressed_sha256": _sha256(context_compressed),
                    "osm_base_timestamp": context_snapshot.get("osm3s", {}).get(
                        "timestamp_osm_base"
                    ),
                },
            ],
        },
    )


def _load_existing_manifest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.is_file():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        str(source["id"]): source
        for source in manifest.get("sources", [])
        if isinstance(source, dict) and source.get("id")
    }


def _selected_events(
    registry: dict[str, Any], requested: list[str]
) -> list[dict[str, Any]]:
    events = list(registry["events"])
    if not requested:
        return events
    by_id = {str(event["id"]): event for event in events}
    missing = sorted(set(requested) - set(by_id))
    if missing:
        raise SystemExit(f"Unknown event id(s): {', '.join(missing)}")
    return [by_id[event_id] for event_id in requested]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Acquire all 23 events")
    parser.add_argument(
        "--event", action="append", default=[], help="Acquire one event id; repeatable"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Replace existing exact snapshots"
    )
    parser.add_argument(
        "--official-only", action="store_true", help="Skip OSM acquisition"
    )
    parser.add_argument("--osm-only", action="store_true", help="Skip official pages")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--request-interval", type=float, default=1.0)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help=(
            "Manifest used for both verified snapshot reuse and final output; "
            "separate paths permit disjoint parallel acquisition groups"
        ),
    )
    parser.add_argument(
        "--overpass-endpoint",
        action="append",
        default=[],
        help="Override/fallback Overpass endpoint; repeatable",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.official_only and args.osm_only:
        raise SystemExit("--official-only and --osm-only are mutually exclusive")
    if not args.all and not args.event:
        raise SystemExit("Choose --all or at least one --event")

    manifest_path = args.manifest.resolve()
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    selected = _selected_events(registry, list(args.event))
    existing = _load_existing_manifest(manifest_path)
    sources: dict[str, dict[str, Any]] = dict(existing)
    errors: list[dict[str, str]] = []

    if not args.osm_only:
        for source in (
            list(registry["calendar_sources"])
            + list(registry.get("layout_reference_sources", []))
        ):
            try:
                entry = _official_source_entry(
                    source,
                    existing=existing,
                    refresh=args.refresh,
                    user_agent=args.user_agent,
                    timeout=args.timeout,
                )
                sources[entry["id"]] = entry
                print(f"official {entry['id']}: {entry['payload_sha256']}")
            except Exception as exc:  # network failures belong in the ledger
                errors.append(
                    {
                        "source_id": str(source["id"]),
                        "stage": "official-global",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            time.sleep(max(0.0, args.request_interval / 4.0))

        for event in selected:
            try:
                entry = _race_page_entry(
                    event,
                    existing=existing,
                    refresh=args.refresh,
                    user_agent=args.user_agent,
                    timeout=args.timeout,
                )
                sources[entry["id"]] = entry
                print(f"race-page {event['id']}: {entry['payload_sha256']}")
            except Exception as exc:
                errors.append(
                    {
                        "source_id": f"f1-race-page-{event['id']}",
                        "stage": "official-race-page",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            time.sleep(max(0.0, args.request_interval / 4.0))

    if not args.official_only:
        endpoints = tuple(args.overpass_endpoint) or OVERPASS_ENDPOINTS
        for event in selected:
            try:
                entry = _osm_entry(
                    event,
                    existing=existing,
                    refresh=args.refresh,
                    endpoints=endpoints,
                    user_agent=args.user_agent,
                    timeout=args.timeout,
                )
                sources[entry["id"]] = entry
                print(
                    f"osm {event['id']}: {entry['element_count']} elements / "
                    f"{entry['payload_sha256']}"
                )
            except Exception as exc:
                errors.append(
                    {
                        "source_id": f"osm-circuit-context-{event['id']}",
                        "stage": "osm-overpass",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            time.sleep(max(0.0, args.request_interval))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "season": int(registry["season"]),
        "freeze": {
            "frozen_at": registry["frozen_at"],
            "event_registry_path": _relative(REGISTRY_PATH),
            "event_registry_sha256": _sha256(REGISTRY_PATH.read_bytes()),
            "event_count": len(registry["events"]),
            "excluded_event_count": len(registry["excluded_calendar_events"]),
        },
        "generated_at": _utc_now(),
        "network_fallback_for_rendering": False,
        "official_images_acquired": False,
        "official_event_documents_acquired": any(
            source.get("source_kind") == "current-event-circuit-document"
            for source in sources.values()
        ),
        "sources": sorted(sources.values(), key=lambda item: str(item["id"])),
        "acquisition_errors": sorted(
            errors, key=lambda item: (item["source_id"], item["stage"])
        ),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(_canonical_json(manifest))
    print(
        f"wrote {manifest_path}: {len(manifest['sources'])} sources, "
        f"{len(errors)} errors"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
